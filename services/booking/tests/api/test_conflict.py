"""Losing the race for a slot: 409 with somewhere else to go.

The constraint in the database is what refuses the second booking. These tests
prove that its refusal arrives as a domain answer rather than a 500, and that
it is told apart from every other violation of integrity.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, time, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from barber_booking.models.booking import Booking
from barber_booking.models.master_settings import MasterSettings
from barber_common.testing.fixtures import app_client

pytestmark = pytest.mark.integration

MasterSettingsFactory = Callable[..., Awaitable[MasterSettings]]
TemplateFactory = Callable[..., Awaitable[object]]
AuthorizationFactory = Callable[..., dict[str, str]]

BOOKINGS = "/api/v1/bookings"

WORKDAY = datetime.now(UTC).date() + timedelta(days=30)
TEN_MOSCOW = datetime.combine(WORKDAY, time(7), tzinfo=UTC)
SERVICE_ID = uuid4()


async def booked_master(
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
    *,
    buffer_after_min: int = 0,
) -> MasterSettings:
    master = await make_master_settings(buffer_after_min=buffer_after_min)
    await make_template(
        master_id=master.master_id,
        weekday=WORKDAY.weekday(),
        start_time=time(10),
        end_time=time(20),
    )
    return master


async def book(
    app: FastAPI,
    master: MasterSettings,
    start_at: datetime,
    headers: dict[str, str],
    *,
    key: str | None = None,
) -> httpx.Response:
    async with app_client(app) as client:
        return await client.post(
            BOOKINGS,
            json={
                "master_id": str(master.master_id),
                "service_id": str(SERVICE_ID),
                "start_at": start_at.isoformat(),
            },
            headers={**headers, "Idempotency-Key": key or str(uuid4())},
        )


async def active_bookings(session: AsyncSession, master: MasterSettings) -> int:
    statement = (
        select(func.count())
        .select_from(Booking)
        .where(Booking.master_id == master.master_id, Booking.status == "confirmed")
    )
    return int((await session.execute(statement)).scalar_one())


async def test_the_same_slot_twice_is_a_conflict(
    app: FastAPI,
    session: AsyncSession,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await booked_master(make_master_settings, make_template)
    await book(app, master, TEN_MOSCOW, authorize())

    response = await book(app, master, TEN_MOSCOW, authorize())

    assert response.status_code == 409
    assert response.json()["code"] == "slot_taken"
    assert await active_bookings(session, master) == 1


async def test_the_conflict_offers_free_starts_instead(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await booked_master(make_master_settings, make_template)
    await book(app, master, TEN_MOSCOW, authorize())

    response = await book(app, master, TEN_MOSCOW, authorize())

    alternatives = [datetime.fromisoformat(start) for start in response.json()["alternatives"]]
    assert alternatives
    # Later than the moment that was asked for, and free: the first one starts
    # after the booking that won, buffer included.
    assert all(start > TEN_MOSCOW for start in alternatives)
    assert alternatives[0] == TEN_MOSCOW + timedelta(minutes=45)


async def test_an_overlapping_start_is_a_conflict_too(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await booked_master(make_master_settings, make_template)
    await book(app, master, TEN_MOSCOW, authorize())

    # 10:15 with a service of 45 minutes runs into the booking of 10:00.
    response = await book(app, master, TEN_MOSCOW + timedelta(minutes=15), authorize())

    assert response.status_code == 409


async def test_the_buffer_of_the_first_booking_closes_the_next_start(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await booked_master(make_master_settings, make_template, buffer_after_min=15)
    await book(app, master, TEN_MOSCOW, authorize())

    response = await book(app, master, TEN_MOSCOW + timedelta(minutes=45), authorize())

    assert response.status_code == 409
    # The first free start is an hour later: 45 minutes of service plus the
    # buffer of both bookings.
    assert response.json()["alternatives"][0] == (TEN_MOSCOW + timedelta(hours=1)).isoformat()


async def test_the_next_free_start_after_a_booking_is_accepted(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await booked_master(make_master_settings, make_template)
    await book(app, master, TEN_MOSCOW, authorize())

    response = await book(app, master, TEN_MOSCOW + timedelta(minutes=45), authorize())

    assert response.status_code == 201


async def test_two_masters_are_bookable_at_the_same_moment(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    first = await booked_master(make_master_settings, make_template)
    second = await booked_master(make_master_settings, make_template)
    await book(app, first, TEN_MOSCOW, authorize())

    response = await book(app, second, TEN_MOSCOW, authorize())

    assert response.status_code == 201


async def test_a_cancelled_booking_gives_its_slot_back(
    app: FastAPI,
    session: AsyncSession,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await booked_master(make_master_settings, make_template)
    first = await book(app, master, TEN_MOSCOW, authorize())
    await session.execute(
        update(Booking)
        .where(Booking.id == UUID(first.json()["id"]))
        .values(status="cancelled_by_client")
    )
    await session.flush()

    response = await book(app, master, TEN_MOSCOW, authorize())

    assert response.status_code == 201


async def test_a_repeated_key_is_not_reported_as_a_taken_slot(
    app: FastAPI,
    session: AsyncSession,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    """The unique violation of the key and the exclusion of the slot differ."""
    master = await booked_master(make_master_settings, make_template)
    headers = authorize()
    key = str(uuid4())
    first = await book(app, master, TEN_MOSCOW, headers, key=key)

    again = await book(app, master, TEN_MOSCOW, headers, key=key)

    assert again.status_code == 201
    assert again.json() == first.json()
    assert await active_bookings(session, master) == 1
