"""POST /api/v1/bookings/{id}/reschedule through the real ASGI stack."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, time, timedelta
from typing import Protocol
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from barber_booking.models.booking import Booking
from barber_booking.models.master_settings import MasterSettings
from barber_common.db.session import transaction
from barber_common.events.bookings import BookingEventType
from barber_common.outbox.models import OutboxMessage
from barber_common.testing.fixtures import app_client

pytestmark = pytest.mark.integration


class CatalogStub(Protocol):
    """The part of the fake catalog these tests bend."""

    master_active: bool
    calls: int


MasterSettingsFactory = Callable[..., Awaitable[MasterSettings]]
TemplateFactory = Callable[..., Awaitable[object]]
BookingFactory = Callable[..., Awaitable[Booking]]
AuthorizationFactory = Callable[..., dict[str, str]]

# A month ahead: past the lead time, inside the horizon. The master works ten
# to eight in Moscow, which is seven to five in UTC.
WORKDAY = datetime.now(UTC).date() + timedelta(days=30)
SERVICE_ID = uuid4()


def moscow(hour: int, minute: int = 0) -> datetime:
    """A wall-clock time of the workday in Moscow, as an instant."""
    return datetime.combine(WORKDAY, time(hour - 3, minute), tzinfo=UTC)


async def working_master(
    make_master_settings: MasterSettingsFactory, make_template: TemplateFactory
) -> MasterSettings:
    master = await make_master_settings()
    await make_template(master_id=master.master_id, weekday=WORKDAY.weekday())
    return master


async def book(
    app: FastAPI, master: MasterSettings, start_at: datetime, headers: dict[str, str]
) -> UUID:
    """A booking made the way a client makes one."""
    async with app_client(app) as client:
        response = await client.post(
            "/api/v1/bookings",
            json={
                "master_id": str(master.master_id),
                "service_id": str(SERVICE_ID),
                "start_at": start_at.isoformat(),
            },
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


async def reschedule(
    app: FastAPI, booking_id: UUID, start_at: datetime | str, headers: dict[str, str]
) -> httpx.Response:
    value = start_at if isinstance(start_at, str) else start_at.isoformat()
    async with app_client(app) as client:
        return await client.post(
            f"/api/v1/bookings/{booking_id}/reschedule",
            json={"start_at": value},
            headers=headers,
        )


async def stored(session: AsyncSession, booking_id: UUID) -> Booking:
    statement = (
        select(Booking).where(Booking.id == booking_id).execution_options(populate_existing=True)
    )
    return (await session.execute(statement)).scalar_one()


async def moves(session: AsyncSession) -> list[OutboxMessage]:
    statement = select(OutboxMessage).where(
        OutboxMessage.event_type == BookingEventType.RESCHEDULED.value
    )
    return list((await session.execute(statement)).scalars().all())


# --- the move ---------------------------------------------------------------


async def test_the_client_moves_a_booking_and_it_stays_the_same_booking(
    app: FastAPI,
    session: AsyncSession,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await working_master(make_master_settings, make_template)
    client = authorize()
    booking_id = await book(app, master, moscow(10), client)

    response = await reschedule(app, booking_id, moscow(14), client)

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(booking_id)
    assert datetime.fromisoformat(body["start_at"]) == moscow(14)
    assert datetime.fromisoformat(body["end_at"]) == moscow(14, 45)
    row = await stored(session, booking_id)
    assert (row.start_at, row.status) == (moscow(14), "confirmed")


async def test_the_move_is_announced_with_the_old_time_and_the_new_one(
    app: FastAPI,
    session: AsyncSession,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await working_master(make_master_settings, make_template)
    client = authorize()
    booking_id = await book(app, master, moscow(10), client)

    await reschedule(app, booking_id, moscow(14), client)

    events = await moves(session)
    assert len(events) == 1
    payload = events[0].payload
    assert datetime.fromisoformat(str(payload["previous_start_at"])) == moscow(10)
    assert datetime.fromisoformat(str(payload["start_at"])) == moscow(14)


async def test_the_reminder_is_counted_again_and_due_again(
    app: FastAPI,
    session: AsyncSession,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await working_master(make_master_settings, make_template)
    client = authorize()
    booking_id = await book(app, master, moscow(10), client)
    async with transaction(session):
        await session.execute(
            update(Booking)
            .where(Booking.id == booking_id)
            .values(reminder_sent_at=datetime.now(UTC))
        )

    await reschedule(app, booking_id, moscow(14), client)

    row = await stored(session, booking_id)
    assert row.reminder_at == moscow(14) - timedelta(hours=4)
    assert row.reminder_sent_at is None


async def test_a_move_may_overlap_the_time_the_booking_itself_leaves(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await working_master(make_master_settings, make_template)
    client = authorize()
    booking_id = await book(app, master, moscow(10), client)

    response = await reschedule(app, booking_id, moscow(10, 15), client)

    assert response.status_code == 200


async def test_moving_to_the_same_time_answers_without_asking_anyone(
    app: FastAPI,
    session: AsyncSession,
    catalog: CatalogStub,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await working_master(make_master_settings, make_template)
    client = authorize()
    booking_id = await book(app, master, moscow(10), client)
    calls = catalog.calls

    response = await reschedule(app, booking_id, moscow(10), client)

    assert response.status_code == 200
    assert catalog.calls == calls
    assert await moves(session) == []


# --- losing the new time ----------------------------------------------------


async def test_a_taken_time_is_409_and_the_booking_keeps_its_old_time(
    app: FastAPI,
    session: AsyncSession,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
    make_booking: BookingFactory,
) -> None:
    master = await working_master(make_master_settings, make_template)
    client = authorize()
    booking_id = await book(app, master, moscow(10), client)
    await make_booking(master_id=master.master_id, start_at=moscow(14))

    response = await reschedule(app, booking_id, moscow(14), client)

    assert response.status_code == 409
    assert response.json()["code"] == "slot_taken"
    assert response.json()["alternatives"]
    row = await stored(session, booking_id)
    assert (row.start_at, row.status) == (moscow(10), "confirmed")
    assert await moves(session) == []


async def test_the_alternatives_do_not_count_the_booking_as_its_own_obstacle(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
    make_booking: BookingFactory,
) -> None:
    # This booking holds 12:00-12:45; somebody else holds 11:00-11:45. Asked
    # for 11:15, the nearest free start is 11:45 -- which overlaps only the
    # time this booking is about to leave.
    master = await working_master(make_master_settings, make_template)
    client = authorize()
    booking_id = await book(app, master, moscow(12), client)
    await make_booking(master_id=master.master_id, start_at=moscow(11))

    response = await reschedule(app, booking_id, moscow(11, 15), client)

    assert response.status_code == 409
    first = datetime.fromisoformat(response.json()["alternatives"][0])
    assert first == moscow(11, 45)


# --- the rules of a new booking apply to the new time ----------------------


async def test_a_new_time_the_master_does_not_work_is_refused(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await working_master(make_master_settings, make_template)
    client = authorize()
    booking_id = await book(app, master, moscow(10), client)

    response = await reschedule(app, booking_id, moscow(21), client)

    assert response.status_code == 422
    assert response.json()["code"] == "slot_outside_schedule"


async def test_a_new_time_too_close_to_now_is_refused(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await working_master(make_master_settings, make_template)
    client = authorize()
    booking_id = await book(app, master, moscow(10), client)

    response = await reschedule(app, booking_id, datetime.now(UTC) + timedelta(hours=1), client)

    assert response.status_code == 422
    assert response.json()["code"] == "booking_too_late"


async def test_a_master_deactivated_since_takes_no_moves(
    app: FastAPI,
    catalog: CatalogStub,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await working_master(make_master_settings, make_template)
    client = authorize()
    booking_id = await book(app, master, moscow(10), client)
    catalog.master_active = False

    response = await reschedule(app, booking_id, moscow(14), client)

    assert response.status_code == 422
    assert response.json()["code"] == "master_inactive"


# --- the deadline -----------------------------------------------------------


async def test_the_client_cannot_move_after_the_deadline(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
    make_booking: BookingFactory,
) -> None:
    master = await working_master(make_master_settings, make_template)
    client_id = uuid4()
    soon = datetime.now(UTC) + timedelta(hours=3)
    booking = await make_booking(
        master_id=master.master_id, start_at=soon, client_user_id=client_id
    )

    response = await reschedule(app, booking.id, moscow(14), authorize(user_id=client_id))

    assert response.status_code == 422
    assert response.json()["code"] == "cancel_deadline_passed"


async def test_the_salon_moves_after_the_deadline(
    app: FastAPI,
    catalog: CatalogStub,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
    make_booking: BookingFactory,
) -> None:
    master = await working_master(make_master_settings, make_template)
    soon = datetime.now(UTC) + timedelta(hours=3)
    booking = await make_booking(
        master_id=master.master_id, salon_id=master.salon_id, start_at=soon
    )

    response = await reschedule(
        app, booking.id, moscow(14), authorize(roles=(("salon_admin", master.salon_id),))
    )

    assert response.status_code == 200


async def test_a_cancelled_booking_cannot_be_moved(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_booking: BookingFactory,
) -> None:
    client_id = uuid4()
    booking = await make_booking(
        master_id=uuid4(),
        start_at=moscow(10),
        client_user_id=client_id,
        status="cancelled_by_client",
    )

    response = await reschedule(app, booking.id, moscow(14), authorize(user_id=client_id))

    assert response.status_code == 409
    assert response.json()["code"] == "booking_status_conflict"


# --- who may not ------------------------------------------------------------


async def test_another_client_cannot_move_the_booking(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await working_master(make_master_settings, make_template)
    booking_id = await book(app, master, moscow(10), authorize())

    response = await reschedule(app, booking_id, moscow(14), authorize())

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden_for_role"


async def test_the_master_of_the_booking_cannot_move_it(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await working_master(make_master_settings, make_template)
    booking_id = await book(app, master, moscow(10), authorize())

    response = await reschedule(
        app,
        booking_id,
        moscow(14),
        authorize(roles=(("master", master.salon_id),), user_id=master.user_id),
    )

    assert response.status_code == 403


async def test_an_unknown_booking_is_not_found(
    app: FastAPI, authorize: AuthorizationFactory
) -> None:
    response = await reschedule(app, uuid4(), moscow(14), authorize())

    assert response.status_code == 404


async def test_a_caller_without_a_token_is_refused(app: FastAPI) -> None:
    response = await reschedule(app, uuid4(), moscow(14), {})

    assert response.status_code == 401


async def test_a_time_without_an_offset_is_refused(
    app: FastAPI, authorize: AuthorizationFactory
) -> None:
    response = await reschedule(app, uuid4(), "2026-10-05T10:00:00", authorize())

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
