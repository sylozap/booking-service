"""POST /api/v1/bookings/{id}/complete and /no-show through the real ASGI stack."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from barber_booking.models.booking import Booking
from barber_booking.models.master_settings import MasterSettings
from barber_common.db.errors import SQLSTATE_EXCLUSION_VIOLATION, sqlstate_of
from barber_common.outbox.models import OutboxMessage
from barber_common.testing.fixtures import app_client

pytestmark = pytest.mark.integration

MasterSettingsFactory = Callable[..., Awaitable[MasterSettings]]
BookingFactory = Callable[..., Awaitable[Booking]]
AuthorizationFactory = Callable[..., dict[str, str]]

COMPLETE = "complete"
NO_SHOW = "no-show"


def started(minutes_ago: int = 20) -> datetime:
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).replace(microsecond=0)


async def close(
    app: FastAPI, booking_id: UUID | str, action: str, headers: dict[str, str]
) -> httpx.Response:
    async with app_client(app) as client:
        return await client.post(f"/api/v1/bookings/{booking_id}/{action}", headers=headers)


def as_master(authorize: AuthorizationFactory, master: MasterSettings) -> dict[str, str]:
    """The token of the master of a booking: their account, the role in their salon."""
    return authorize(roles=(("client", None), ("master", master.salon_id)), user_id=master.user_id)


async def events_of(session: AsyncSession, booking_id: UUID) -> list[str]:
    statement = select(OutboxMessage.event_type).where(OutboxMessage.aggregate_id == booking_id)
    return [str(event_type) for event_type in (await session.execute(statement)).scalars()]


async def a_started_visit(
    make_master_settings: MasterSettingsFactory,
    make_booking: BookingFactory,
    *,
    status: str = "confirmed",
    start_at: datetime | None = None,
) -> tuple[MasterSettings, Booking]:
    master = await make_master_settings()
    booking = await make_booking(
        master_id=master.master_id,
        salon_id=master.salon_id,
        start_at=start_at or started(),
        status=status,
    )
    return master, booking


# --- recording the outcome --------------------------------------------------


async def test_the_master_of_the_booking_completes_a_visit(
    app: FastAPI,
    session: AsyncSession,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_booking: BookingFactory,
) -> None:
    master, booking = await a_started_visit(make_master_settings, make_booking)

    response = await close(app, booking.id, COMPLETE, as_master(authorize, master))

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert await events_of(session, booking.id) == ["booking.completed"]


async def test_an_admin_of_the_salon_marks_a_no_show(
    app: FastAPI,
    session: AsyncSession,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_booking: BookingFactory,
) -> None:
    master, booking = await a_started_visit(make_master_settings, make_booking)

    response = await close(
        app, booking.id, NO_SHOW, authorize(roles=(("salon_admin", master.salon_id),))
    )

    assert response.status_code == 200
    assert response.json()["status"] == "no_show"
    assert await events_of(session, booking.id) == ["booking.no_show"]


async def test_a_visit_still_ahead_cannot_be_completed(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_booking: BookingFactory,
) -> None:
    master, booking = await a_started_visit(
        make_master_settings, make_booking, start_at=datetime.now(UTC) + timedelta(hours=2)
    )

    response = await close(app, booking.id, COMPLETE, as_master(authorize, master))

    assert response.status_code == 422
    assert response.json()["code"] == "booking_not_started"


async def test_a_no_show_keeps_the_time_taken(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_booking: BookingFactory,
) -> None:
    master, booking = await a_started_visit(make_master_settings, make_booking)
    await close(app, booking.id, NO_SHOW, as_master(authorize, master))

    with pytest.raises(IntegrityError) as failure:
        await make_booking(master_id=master.master_id, start_at=booking.start_at)

    assert sqlstate_of(failure.value) == SQLSTATE_EXCLUSION_VIOLATION


async def test_completing_twice_answers_twice_and_announces_once(
    app: FastAPI,
    session: AsyncSession,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_booking: BookingFactory,
) -> None:
    master, booking = await a_started_visit(make_master_settings, make_booking)

    first = await close(app, booking.id, COMPLETE, as_master(authorize, master))
    second = await close(app, booking.id, COMPLETE, as_master(authorize, master))

    assert (first.status_code, second.status_code) == (200, 200)
    assert await events_of(session, booking.id) == ["booking.completed"]


async def test_a_completed_visit_is_not_turned_into_a_no_show(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_booking: BookingFactory,
) -> None:
    master, booking = await a_started_visit(make_master_settings, make_booking, status="completed")

    response = await close(app, booking.id, NO_SHOW, as_master(authorize, master))

    assert response.status_code == 409
    assert response.json()["code"] == "booking_status_conflict"


async def test_a_cancelled_booking_cannot_be_completed(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_booking: BookingFactory,
) -> None:
    master, booking = await a_started_visit(
        make_master_settings, make_booking, status="cancelled_by_salon"
    )

    response = await close(app, booking.id, COMPLETE, as_master(authorize, master))

    assert response.status_code == 409


# --- who may not ------------------------------------------------------------


async def test_the_client_cannot_mark_their_own_visit(
    app: FastAPI,
    session: AsyncSession,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_booking: BookingFactory,
) -> None:
    master = await make_master_settings()
    client_id = uuid4()
    booking = await make_booking(
        master_id=master.master_id, start_at=started(), client_user_id=client_id
    )

    response = await close(app, booking.id, COMPLETE, authorize(user_id=client_id))

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden_for_role"
    assert await events_of(session, booking.id) == []


async def test_another_master_of_the_salon_cannot_mark_the_visit(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_booking: BookingFactory,
) -> None:
    master, booking = await a_started_visit(make_master_settings, make_booking)
    colleague = await make_master_settings(salon_id=master.salon_id)

    response = await close(app, booking.id, COMPLETE, as_master(authorize, colleague))

    assert response.status_code == 403


async def test_the_account_of_the_master_without_the_role_cannot_mark_it(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_booking: BookingFactory,
) -> None:
    # The master role was taken away; the account alone is not enough.
    master, booking = await a_started_visit(make_master_settings, make_booking)

    response = await close(app, booking.id, COMPLETE, authorize(user_id=master.user_id))

    assert response.status_code == 403


async def test_an_admin_of_another_salon_cannot_mark_the_visit(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_booking: BookingFactory,
) -> None:
    _, booking = await a_started_visit(make_master_settings, make_booking)

    response = await close(app, booking.id, NO_SHOW, authorize(roles=(("salon_admin", uuid4()),)))

    assert response.status_code == 403


async def test_an_unknown_booking_is_not_found(
    app: FastAPI, authorize: AuthorizationFactory
) -> None:
    response = await close(app, uuid4(), COMPLETE, authorize(roles=(("super_admin", None),)))

    assert response.status_code == 404


@pytest.mark.parametrize("action", [COMPLETE, NO_SHOW])
async def test_a_caller_without_a_token_is_refused(app: FastAPI, action: str) -> None:
    response = await close(app, uuid4(), action, {})

    assert response.status_code == 401


async def test_an_identifier_that_is_not_a_uuid_is_refused(
    app: FastAPI, authorize: AuthorizationFactory
) -> None:
    response = await close(app, "not-a-uuid", COMPLETE, authorize())

    assert response.status_code == 422
