"""POST /api/v1/bookings through the real ASGI stack."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Protocol
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from barber_booking.models.booking import Booking
from barber_booking.models.master_settings import MasterSettings
from barber_common.events.bookings import BOOKINGS_TOPIC, BookingEventType
from barber_common.outbox.models import OutboxMessage
from barber_common.testing.fixtures import app_client

pytestmark = pytest.mark.integration


class CatalogStub(Protocol):
    """The part of the fake catalog these tests bend."""

    master_active: bool
    duration_min: int
    booking_min_lead_min: int
    booking_horizon_days: int
    cancel_deadline_min: int
    answers: str
    service_name: str
    price: str


MasterSettingsFactory = Callable[..., Awaitable[MasterSettings]]
TemplateFactory = Callable[..., Awaitable[object]]
AuthorizationFactory = Callable[..., dict[str, str]]

BOOKINGS = "/api/v1/bookings"

# A month ahead: past the default lead time, inside the default horizon.
WORKDAY = datetime.now(UTC).date() + timedelta(days=30)
# 10:00 in Moscow, the first start of the day in the template below.
TEN_MOSCOW = datetime.combine(WORKDAY, time(7), tzinfo=UTC)
SERVICE_ID = uuid4()


async def booked_master(
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
    *,
    buffer_after_min: int = 0,
) -> MasterSettings:
    """A master working ten to eight on the day under test."""
    master = await make_master_settings(buffer_after_min=buffer_after_min)
    await make_template(
        master_id=master.master_id,
        weekday=WORKDAY.weekday(),
        start_time=time(10),
        end_time=time(20),
    )
    return master


def a_booking(master: MasterSettings, **overrides: object) -> dict[str, object]:
    """A valid body a test can bend one field of."""
    body: dict[str, object] = {
        "master_id": str(master.master_id),
        "service_id": str(SERVICE_ID),
        "start_at": TEN_MOSCOW.isoformat(),
    }
    body.update(overrides)
    return body


async def book(
    app: FastAPI,
    body: dict[str, object],
    headers: dict[str, str],
    *,
    key: str | None = None,
) -> httpx.Response:
    async with app_client(app) as client:
        return await client.post(
            BOOKINGS,
            json=body,
            headers={**headers, "Idempotency-Key": key or str(uuid4())},
        )


async def bookings_of(session: AsyncSession, master_id: UUID) -> int:
    statement = select(func.count()).select_from(Booking).where(Booking.master_id == master_id)
    return int((await session.execute(statement)).scalar_one())


async def queued_events(session: AsyncSession) -> list[OutboxMessage]:
    statement = select(OutboxMessage).order_by(OutboxMessage.created_at)
    return list((await session.execute(statement)).scalars().all())


# --- the happy path ---------------------------------------------------------


async def test_a_client_books_a_master_and_the_snapshot_is_kept(
    app: FastAPI,
    session: AsyncSession,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await booked_master(make_master_settings, make_template, buffer_after_min=15)
    client_id = uuid4()

    response = await book(app, a_booking(master), authorize(user_id=client_id))

    assert response.status_code == 201
    body = response.json()
    assert body["client_user_id"] == str(client_id)
    assert body["service_name"] == "Haircut"
    assert body["price"] == "3500.00"
    assert body["duration_min"] == 45
    assert body["buffer_min"] == 15
    assert body["status"] == "confirmed"
    assert datetime.fromisoformat(body["end_at"]) == TEN_MOSCOW + timedelta(minutes=45)


async def test_the_booking_is_stored_with_a_reminder(
    app: FastAPI,
    session: AsyncSession,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await booked_master(make_master_settings, make_template)

    response = await book(app, a_booking(master), authorize())

    stored = await session.get(Booking, UUID(response.json()["id"]))
    assert stored is not None
    assert stored.price == Decimal("3500.00")
    assert stored.reminder_at == TEN_MOSCOW - timedelta(hours=4)


async def test_the_salon_cancel_deadline_is_kept_with_the_booking(
    app: FastAPI,
    session: AsyncSession,
    catalog: CatalogStub,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await booked_master(make_master_settings, make_template)
    catalog.cancel_deadline_min = 180

    response = await book(app, a_booking(master), authorize())

    stored = await session.get(Booking, UUID(response.json()["id"]))
    assert stored is not None
    assert stored.cancel_deadline_min == 180


async def test_creating_a_booking_queues_exactly_one_event(
    app: FastAPI,
    session: AsyncSession,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await booked_master(make_master_settings, make_template)

    response = await book(app, a_booking(master), authorize())

    [event] = await queued_events(session)
    assert event.topic == BOOKINGS_TOPIC
    assert event.event_type == BookingEventType.CREATED.value
    # The partitioning key: the events of one booking stay in order.
    assert str(event.aggregate_id) == response.json()["id"]
    assert event.payload["service_name"] == "Haircut"
    assert "email" not in event.payload
    # The salon's zone, so the client is told the time where they are.
    assert event.payload["timezone"] == "Europe/Moscow"


async def test_a_later_start_of_the_same_day_is_bookable_too(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await booked_master(make_master_settings, make_template)

    response = await book(
        app,
        a_booking(master, start_at=(TEN_MOSCOW + timedelta(hours=3)).isoformat()),
        authorize(),
    )

    assert response.status_code == 201


# --- idempotency ------------------------------------------------------------


async def test_the_same_key_and_body_answer_the_same_booking(
    app: FastAPI,
    session: AsyncSession,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await booked_master(make_master_settings, make_template)
    headers = authorize()
    key = str(uuid4())

    first = await book(app, a_booking(master), headers, key=key)
    second = await book(app, a_booking(master), headers, key=key)

    assert second.status_code == 201
    assert second.json() == first.json()
    assert await bookings_of(session, master.master_id) == 1


async def test_the_same_key_with_another_body_is_refused(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await booked_master(make_master_settings, make_template)
    headers = authorize()
    key = str(uuid4())
    await book(app, a_booking(master), headers, key=key)

    response = await book(
        app,
        a_booking(master, start_at=(TEN_MOSCOW + timedelta(hours=2)).isoformat()),
        headers,
        key=key,
    )

    assert response.status_code == 422
    assert response.json()["code"] == "idempotency_key_reuse"


async def test_a_request_without_the_header_is_refused(
    app: FastAPI,
    session: AsyncSession,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await booked_master(make_master_settings, make_template)

    async with app_client(app) as client:
        response = await client.post(BOOKINGS, json=a_booking(master), headers=authorize())

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert await bookings_of(session, master.master_id) == 0


async def test_two_clients_may_use_the_same_key(
    app: FastAPI,
    session: AsyncSession,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await booked_master(make_master_settings, make_template)
    key = "the-same-key"
    await book(app, a_booking(master), authorize(), key=key)

    response = await book(
        app,
        a_booking(master, start_at=(TEN_MOSCOW + timedelta(hours=2)).isoformat()),
        authorize(),
        key=key,
    )

    assert response.status_code == 201
    assert await bookings_of(session, master.master_id) == 2


# --- the windows ------------------------------------------------------------


async def test_a_booking_inside_the_lead_time_is_refused(
    app: FastAPI,
    catalog: CatalogStub,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await booked_master(make_master_settings, make_template)
    # A salon that wants two months of notice: the day under test is inside it.
    catalog.booking_min_lead_min = 60 * 24 * 60

    response = await book(app, a_booking(master), authorize())

    assert response.status_code == 422
    assert response.json()["code"] == "booking_too_late"


async def test_a_booking_beyond_the_horizon_is_refused(
    app: FastAPI,
    catalog: CatalogStub,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await booked_master(make_master_settings, make_template)
    catalog.booking_horizon_days = 7

    response = await book(app, a_booking(master), authorize())

    assert response.status_code == 422
    assert response.json()["code"] == "booking_too_far"


async def test_a_start_outside_working_hours_is_refused(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await booked_master(make_master_settings, make_template)

    response = await book(
        app,
        a_booking(master, start_at=(TEN_MOSCOW - timedelta(hours=2)).isoformat()),
        authorize(),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "slot_outside_schedule"


async def test_a_start_off_the_grid_is_refused(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await booked_master(make_master_settings, make_template)

    response = await book(
        app,
        a_booking(master, start_at=(TEN_MOSCOW + timedelta(minutes=7)).isoformat()),
        authorize(),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "slot_outside_schedule"


async def test_a_master_without_a_schedule_has_no_bookable_time(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
) -> None:
    master = await make_master_settings()

    response = await book(app, a_booking(master), authorize())

    assert response.status_code == 422
    assert response.json()["code"] == "slot_outside_schedule"


async def test_a_deactivated_master_takes_no_bookings(
    app: FastAPI,
    catalog: CatalogStub,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await booked_master(make_master_settings, make_template)
    catalog.master_active = False

    response = await book(app, a_booking(master), authorize())

    assert response.status_code == 422
    assert response.json()["code"] == "master_inactive"


async def test_a_service_the_master_does_not_offer_is_refused(
    app: FastAPI,
    catalog: CatalogStub,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await booked_master(make_master_settings, make_template)
    catalog.answers = "not_offered"

    response = await book(app, a_booking(master), authorize())

    assert response.status_code == 422
    assert response.json()["code"] == "service_not_offered"


async def test_a_catalog_that_is_down_refuses_the_booking(
    app: FastAPI,
    catalog: CatalogStub,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await booked_master(make_master_settings, make_template)
    catalog.answers = "gone"

    response = await book(app, a_booking(master), authorize())

    assert response.status_code == 503


async def test_a_refused_booking_leaves_neither_a_row_nor_an_event(
    app: FastAPI,
    session: AsyncSession,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await booked_master(make_master_settings, make_template)

    await book(
        app,
        a_booking(master, start_at=(TEN_MOSCOW + timedelta(minutes=7)).isoformat()),
        authorize(),
    )

    assert await bookings_of(session, master.master_id) == 0
    assert await queued_events(session) == []


# --- who may ----------------------------------------------------------------


async def test_an_anonymous_caller_cannot_book(
    app: FastAPI,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await booked_master(make_master_settings, make_template)

    response = await book(app, a_booking(master), {})

    assert response.status_code == 401


async def test_a_caller_without_the_client_role_cannot_book(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await booked_master(make_master_settings, make_template)

    response = await book(app, a_booking(master), authorize(roles=(("master", uuid4()),)))

    assert response.status_code == 403


async def test_the_booking_belongs_to_the_caller_of_the_token(
    app: FastAPI,
    session: AsyncSession,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await booked_master(make_master_settings, make_template)
    client_id = uuid4()

    response = await book(
        app,
        # Naming somebody else is the salon's power, not a client's.
        {**a_booking(master), "client_user_id": str(uuid4())},
        authorize(user_id=client_id),
    )

    assert response.status_code == 403
    assert await bookings_of(session, master.master_id) == 0
