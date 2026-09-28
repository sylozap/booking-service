"""POST /api/v1/bookings/{id}/cancel through the real ASGI stack."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, time, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from barber_booking.models.booking import Booking
from barber_booking.models.master_settings import MasterSettings
from barber_common.cache import Cache
from barber_common.events.bookings import BookingEventType
from barber_common.outbox.models import OutboxMessage
from barber_common.testing.fixtures import app_client

pytestmark = pytest.mark.integration

MasterSettingsFactory = Callable[..., Awaitable[MasterSettings]]
TemplateFactory = Callable[..., Awaitable[object]]
BookingFactory = Callable[..., Awaitable[Booking]]
AuthorizationFactory = Callable[..., dict[str, str]]

# A month ahead, ten in the morning in Moscow: clear of every window.
WORKDAY = datetime.now(UTC).date() + timedelta(days=30)
TEN_MOSCOW = datetime.combine(WORKDAY, time(7), tzinfo=UTC)
SERVICE_ID = uuid4()


def in_hours(hours: float) -> datetime:
    """A start this far from now, on the quarter hour it falls in."""
    moment = datetime.now(UTC) + timedelta(hours=hours)
    return moment.replace(minute=moment.minute // 15 * 15, second=0, microsecond=0)


async def cancel(
    app: FastAPI,
    booking_id: UUID,
    headers: dict[str, str],
    body: dict[str, object] | None = None,
) -> httpx.Response:
    async with app_client(app) as client:
        return await client.post(
            f"/api/v1/bookings/{booking_id}/cancel", json=body, headers=headers
        )


async def cancellation_events(session: AsyncSession) -> list[OutboxMessage]:
    statement = select(OutboxMessage).where(
        OutboxMessage.event_type == BookingEventType.CANCELLED.value
    )
    return list((await session.execute(statement)).scalars().all())


async def status_of(session: AsyncSession, booking_id: UUID) -> str:
    statement = select(Booking.status).where(Booking.id == booking_id)
    return str((await session.execute(statement)).scalar_one())


# --- the client -------------------------------------------------------------


async def test_the_client_cancels_five_hours_ahead_of_a_four_hour_deadline(
    app: FastAPI,
    session: AsyncSession,
    authorize: AuthorizationFactory,
    make_booking: BookingFactory,
) -> None:
    client_id = uuid4()
    booking = await make_booking(
        master_id=uuid4(),
        start_at=in_hours(5.5),
        client_user_id=client_id,
        cancel_deadline_min=240,
    )

    response = await cancel(
        app, booking.id, authorize(user_id=client_id), {"reason": "Something came up"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "cancelled_by_client"
    assert body["cancel_reason"] == "Something came up"
    assert body["cancelled_at"] is not None
    assert await status_of(session, booking.id) == "cancelled_by_client"


async def test_the_cancellation_queues_one_event_saying_the_client_did_it(
    app: FastAPI,
    session: AsyncSession,
    authorize: AuthorizationFactory,
    make_booking: BookingFactory,
) -> None:
    client_id = uuid4()
    booking = await make_booking(master_id=uuid4(), start_at=in_hours(24), client_user_id=client_id)

    await cancel(app, booking.id, authorize(user_id=client_id))

    events = await cancellation_events(session)
    assert len(events) == 1
    assert events[0].aggregate_id == booking.id
    assert events[0].payload["cancelled_by"] == "client"
    assert events[0].payload["client_user_id"] == str(client_id)


async def test_the_cancellation_tells_the_time_in_the_zone_of_the_salon(
    app: FastAPI,
    session: AsyncSession,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_booking: BookingFactory,
) -> None:
    master = await make_master_settings(timezone="Asia/Yekaterinburg")
    client_id = uuid4()
    booking = await make_booking(
        master_id=master.master_id, start_at=in_hours(24), client_user_id=client_id
    )

    await cancel(app, booking.id, authorize(user_id=client_id))

    [event] = await cancellation_events(session)
    assert event.payload["timezone"] == "Asia/Yekaterinburg"


async def test_a_master_booking_has_no_settings_for_is_cancelled_without_a_zone(
    app: FastAPI,
    session: AsyncSession,
    authorize: AuthorizationFactory,
    make_booking: BookingFactory,
) -> None:
    client_id = uuid4()
    booking = await make_booking(master_id=uuid4(), start_at=in_hours(24), client_user_id=client_id)

    await cancel(app, booking.id, authorize(user_id=client_id))

    [event] = await cancellation_events(session)
    assert event.payload["timezone"] is None


async def test_the_client_is_refused_three_hours_ahead_of_a_four_hour_deadline(
    app: FastAPI,
    session: AsyncSession,
    authorize: AuthorizationFactory,
    make_booking: BookingFactory,
) -> None:
    client_id = uuid4()
    booking = await make_booking(
        master_id=uuid4(),
        start_at=in_hours(3),
        client_user_id=client_id,
        cancel_deadline_min=240,
    )

    response = await cancel(app, booking.id, authorize(user_id=client_id))

    assert response.status_code == 422
    assert response.json()["code"] == "cancel_deadline_passed"
    assert await status_of(session, booking.id) == "confirmed"
    assert await cancellation_events(session) == []


async def test_the_deadline_is_the_one_the_booking_was_made_under(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_booking: BookingFactory,
) -> None:
    # The salon has four hours today, but this booking was made under one.
    client_id = uuid4()
    booking = await make_booking(
        master_id=uuid4(),
        start_at=in_hours(3),
        client_user_id=client_id,
        cancel_deadline_min=60,
    )

    response = await cancel(app, booking.id, authorize(user_id=client_id))

    assert response.status_code == 200


# --- the salon --------------------------------------------------------------


async def test_an_admin_of_the_salon_cancels_after_the_deadline(
    app: FastAPI,
    session: AsyncSession,
    authorize: AuthorizationFactory,
    make_booking: BookingFactory,
) -> None:
    salon_id = uuid4()
    booking = await make_booking(master_id=uuid4(), start_at=in_hours(3), salon_id=salon_id)

    response = await cancel(
        app, booking.id, authorize(roles=(("client", None), ("salon_admin", salon_id)))
    )

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled_by_salon"
    events = await cancellation_events(session)
    assert events[0].payload["cancelled_by"] == "salon"


async def test_a_super_admin_cancels_as_the_salon(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_booking: BookingFactory,
) -> None:
    booking = await make_booking(master_id=uuid4(), start_at=in_hours(3))

    response = await cancel(app, booking.id, authorize(roles=(("super_admin", None),)))

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled_by_salon"


async def test_nobody_cancels_a_visit_that_has_started(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_booking: BookingFactory,
) -> None:
    booking = await make_booking(master_id=uuid4(), start_at=in_hours(-0.5))

    response = await cancel(app, booking.id, authorize(roles=(("super_admin", None),)))

    assert response.status_code == 422
    assert response.json()["code"] == "booking_already_started"


@pytest.mark.parametrize("status", ["completed", "no_show"])
async def test_a_closed_visit_cannot_be_cancelled(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_booking: BookingFactory,
    status: str,
) -> None:
    booking = await make_booking(master_id=uuid4(), start_at=in_hours(-2), status=status)

    response = await cancel(app, booking.id, authorize(roles=(("super_admin", None),)))

    assert response.status_code == 409
    assert response.json()["code"] == "booking_status_conflict"


# --- who may not ------------------------------------------------------------


async def test_another_client_cannot_cancel_the_booking(
    app: FastAPI,
    session: AsyncSession,
    authorize: AuthorizationFactory,
    make_booking: BookingFactory,
) -> None:
    booking = await make_booking(master_id=uuid4(), start_at=in_hours(24))

    response = await cancel(app, booking.id, authorize())

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden_for_role"
    assert await status_of(session, booking.id) == "confirmed"


async def test_an_admin_of_another_salon_cannot_cancel_the_booking(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_booking: BookingFactory,
) -> None:
    booking = await make_booking(master_id=uuid4(), start_at=in_hours(24))

    response = await cancel(app, booking.id, authorize(roles=(("salon_admin", uuid4()),)))

    assert response.status_code == 403


async def test_the_master_of_the_booking_cannot_cancel_it(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_booking: BookingFactory,
) -> None:
    master = await make_master_settings()
    booking = await make_booking(
        master_id=master.master_id, salon_id=master.salon_id, start_at=in_hours(24)
    )

    response = await cancel(
        app,
        booking.id,
        authorize(roles=(("client", None), ("master", master.salon_id)), user_id=master.user_id),
    )

    assert response.status_code == 403


async def test_an_unknown_booking_is_not_found(
    app: FastAPI, authorize: AuthorizationFactory
) -> None:
    response = await cancel(app, uuid4(), authorize())

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


async def test_a_caller_without_a_token_is_refused(
    app: FastAPI, make_booking: BookingFactory
) -> None:
    booking = await make_booking(master_id=uuid4(), start_at=in_hours(24))

    response = await cancel(app, booking.id, {})

    assert response.status_code == 401


async def test_a_reason_longer_than_allowed_is_refused(
    app: FastAPI, authorize: AuthorizationFactory, make_booking: BookingFactory
) -> None:
    client_id = uuid4()
    booking = await make_booking(master_id=uuid4(), start_at=in_hours(24), client_user_id=client_id)

    response = await cancel(app, booking.id, authorize(user_id=client_id), {"reason": "x" * 501})

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_an_identifier_that_is_not_a_uuid_is_refused(
    app: FastAPI, authorize: AuthorizationFactory
) -> None:
    async with app_client(app) as client:
        response = await client.post("/api/v1/bookings/not-a-uuid/cancel", headers=authorize())

    assert response.status_code == 422


# --- repeating it -----------------------------------------------------------


async def test_cancelling_twice_answers_twice_and_tells_the_client_once(
    app: FastAPI,
    session: AsyncSession,
    authorize: AuthorizationFactory,
    make_booking: BookingFactory,
) -> None:
    client_id = uuid4()
    booking = await make_booking(master_id=uuid4(), start_at=in_hours(24), client_user_id=client_id)

    first = await cancel(app, booking.id, authorize(user_id=client_id))
    second = await cancel(app, booking.id, authorize(user_id=client_id))

    assert (first.status_code, second.status_code) == (200, 200)
    assert second.json() == first.json()
    assert len(await cancellation_events(session)) == 1


# --- the time comes back ----------------------------------------------------


@pytest.fixture
def app_with_cache(app: FastAPI, cache: Cache) -> Iterator[FastAPI]:
    app.state.cache = cache

    yield app

    app.state.cache = None


async def free_starts(app: FastAPI, master_id: UUID) -> list[str]:
    async with app_client(app) as client:
        response = await client.get(
            "/api/v1/availability",
            params={
                "master_id": str(master_id),
                "service_id": str(SERVICE_ID),
                "date_from": WORKDAY.isoformat(),
            },
        )
    assert response.status_code == 200
    return [slot for day in response.json()["days"] for slot in day["slots"]]


async def test_the_cancelled_time_is_offered_again_at_once(
    app_with_cache: FastAPI,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await make_master_settings()
    await make_template(master_id=master.master_id, weekday=WORKDAY.weekday())
    client = authorize()
    async with app_client(app_with_cache) as http:
        created = await http.post(
            "/api/v1/bookings",
            json={
                "master_id": str(master.master_id),
                "service_id": str(SERVICE_ID),
                "start_at": TEN_MOSCOW.isoformat(),
            },
            headers={**client, "Idempotency-Key": str(uuid4())},
        )
    taken = datetime.fromisoformat(created.json()["start_at"])
    # Read once, so the day without this start is what the cache now holds.
    assert all(
        datetime.fromisoformat(slot) != taken
        for slot in await free_starts(app_with_cache, master.master_id)
    )

    await cancel(app_with_cache, UUID(created.json()["id"]), client)

    offered = await free_starts(app_with_cache, master.master_id)
    assert any(datetime.fromisoformat(slot) == taken for slot in offered)
