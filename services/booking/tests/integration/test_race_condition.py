"""The main proof of the project: twenty clients, one slot, one booking.

Real HTTP through the ASGI stack, real transactions on connections of their
own, and a real PostgreSQL deciding who wins. With mocks anywhere in this file
it would prove nothing: the exclusion constraint is the only thing that keeps
two clients from the same chair, and this is where it is held to it.

Every request carries an idempotency key of its own. Sharing one would test
idempotency instead of the race.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, time, timedelta
from typing import Protocol
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from barber_booking.models.booking import Booking
from barber_booking.models.master_settings import MasterSettings
from barber_booking.models.schedule_template import ScheduleTemplate
from barber_common.outbox.models import OutboxMessage
from barber_common.testing.fixtures import app_client

pytestmark = pytest.mark.integration

BOOKINGS = "/api/v1/bookings"

CONTENDERS = 20

WORKDAY = datetime.now(UTC).date() + timedelta(days=30)
TEN_MOSCOW = datetime.combine(WORKDAY, time(7), tzinfo=UTC)
SERVICE_ID = uuid4()


class CatalogStub(Protocol):
    """The part of the fake catalog these tests bend."""

    duration_min: int
    slot_step_min: int


AuthorizationFactory = Callable[..., dict[str, str]]
MasterMaker = Callable[[], Awaitable[MasterSettings]]


@pytest.fixture
async def make_working_master(concurrent_session: AsyncSession) -> MasterMaker:
    """Commit a master who works ten to eight on the day under test."""

    async def factory() -> MasterSettings:
        master = MasterSettings(
            master_id=uuid4(),
            salon_id=uuid4(),
            user_id=uuid4(),
            timezone="Europe/Moscow",
            buffer_after_min=0,
        )
        async with concurrent_session.begin():
            concurrent_session.add(master)
            await concurrent_session.flush()
            concurrent_session.add(
                ScheduleTemplate(
                    master_id=master.master_id,
                    weekday=WORKDAY.weekday(),
                    start_time=time(10),
                    end_time=time(20),
                    valid_from=WORKDAY - timedelta(days=365),
                )
            )
        return master

    return factory


async def book(
    app: FastAPI, master: MasterSettings, start_at: datetime, headers: dict[str, str]
) -> httpx.Response:
    async with app_client(app) as client:
        return await client.post(
            BOOKINGS,
            json={
                "master_id": str(master.master_id),
                "service_id": str(SERVICE_ID),
                "start_at": start_at.isoformat(),
            },
            # A key of its own per request: this is a race, not a retry.
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )


async def storm(
    app: FastAPI,
    attempts: list[tuple[MasterSettings, datetime]],
    authorize: AuthorizationFactory,
) -> list[httpx.Response]:
    """Fire every attempt at once and wait for all of them."""
    return list(
        await asyncio.gather(
            *(book(app, master, start_at, authorize()) for master, start_at in attempts)
        )
    )


async def active_bookings(session: AsyncSession, master_id: UUID) -> int:
    statement = (
        select(func.count())
        .select_from(Booking)
        .where(Booking.master_id == master_id, Booking.status == "confirmed")
    )
    return int((await session.execute(statement)).scalar_one())


async def test_twenty_clients_reaching_for_one_slot_leave_one_booking(
    concurrent_app: FastAPI,
    concurrent_session: AsyncSession,
    authorize: AuthorizationFactory,
    make_working_master: MasterMaker,
) -> None:
    master = await make_working_master()

    responses = await storm(concurrent_app, [(master, TEN_MOSCOW)] * CONTENDERS, authorize)

    created = [response for response in responses if response.status_code == 201]
    conflicts = [response for response in responses if response.status_code == 409]
    assert len(created) == 1
    assert len(conflicts) == CONTENDERS - 1
    assert all(response.json()["code"] == "slot_taken" for response in conflicts)
    assert await active_bookings(concurrent_session, master.master_id) == 1


async def test_the_losers_are_offered_a_free_start(
    concurrent_app: FastAPI,
    authorize: AuthorizationFactory,
    make_working_master: MasterMaker,
) -> None:
    master = await make_working_master()

    responses = await storm(concurrent_app, [(master, TEN_MOSCOW)] * CONTENDERS, authorize)

    conflicts = [response for response in responses if response.status_code == 409]
    # The alternatives are read after the rollback, so they already exclude the
    # booking that won.
    assert all(response.json()["alternatives"] for response in conflicts)


async def test_overlapping_slots_leave_one_booking_too(
    concurrent_app: FastAPI,
    concurrent_session: AsyncSession,
    authorize: AuthorizationFactory,
    make_working_master: MasterMaker,
    catalog: CatalogStub,
) -> None:
    """10:00-11:00 against 10:30-11:30: different starts, one chair."""
    catalog.duration_min = 60
    catalog.slot_step_min = 30
    master = await make_working_master()
    attempts = [
        (master, TEN_MOSCOW if index % 2 == 0 else TEN_MOSCOW + timedelta(minutes=30))
        for index in range(CONTENDERS)
    ]

    responses = await storm(concurrent_app, attempts, authorize)

    created = [response for response in responses if response.status_code == 201]
    assert len(created) == 1
    assert await active_bookings(concurrent_session, master.master_id) == 1


async def test_two_masters_at_the_same_moment_are_both_booked(
    concurrent_app: FastAPI,
    concurrent_session: AsyncSession,
    authorize: AuthorizationFactory,
    make_working_master: MasterMaker,
) -> None:
    first = await make_working_master()
    second = await make_working_master()
    attempts = [(first if index % 2 == 0 else second, TEN_MOSCOW) for index in range(CONTENDERS)]

    responses = await storm(concurrent_app, attempts, authorize)

    created = [response for response in responses if response.status_code == 201]
    assert len(created) == 2
    assert await active_bookings(concurrent_session, first.master_id) == 1
    assert await active_bookings(concurrent_session, second.master_id) == 1


async def test_nothing_but_the_winner_leaves_a_trace(
    concurrent_app: FastAPI,
    concurrent_session: AsyncSession,
    authorize: AuthorizationFactory,
    make_working_master: MasterMaker,
) -> None:
    master = await make_working_master()

    await storm(concurrent_app, [(master, TEN_MOSCOW)] * CONTENDERS, authorize)

    # One booking, and one event about it: the losers rolled back whole.
    bookings = (
        await concurrent_session.execute(select(func.count()).select_from(Booking))
    ).scalar_one()
    events = (
        await concurrent_session.execute(select(func.count()).select_from(OutboxMessage))
    ).scalar_one()
    assert (bookings, events) == (1, 1)
