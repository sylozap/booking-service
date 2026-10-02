"""The time booked over the week ahead, as the business dashboard reads it."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from barber_booking.domain.identifiers import SalonId
from barber_booking.models.booking import Booking
from barber_booking.workers.booked_time import BookedTimeReporter
from barber_common.metrics import REGISTRY

pytestmark = pytest.mark.integration

BookingFactory = Callable[..., Awaitable[Booking]]

# Far from the bookings of other tests, which start around today.
NOW = datetime(2031, 5, 5, 7, 0, tzinfo=UTC)


def reporter(session_factory: async_sessionmaker[AsyncSession]) -> BookedTimeReporter:
    return BookedTimeReporter(session_factory=session_factory, clock=lambda: NOW)


def gauge_of(salon_id: UUID) -> float | None:
    return REGISTRY.get_sample_value("bookings_upcoming_seconds", {"salon": str(salon_id)})


async def test_open_bookings_of_the_week_are_summed_per_salon(
    session_factory: async_sessionmaker[AsyncSession], make_booking: BookingFactory
) -> None:
    salon, other_salon = SalonId(uuid4()), SalonId(uuid4())
    master = uuid4()
    await make_booking(master_id=master, salon_id=salon, start_at=NOW + timedelta(hours=2))
    await make_booking(
        master_id=master,
        salon_id=salon,
        start_at=NOW + timedelta(days=3),
        duration_min=30,
        status="pending",
    )
    await make_booking(master_id=uuid4(), salon_id=other_salon, start_at=NOW + timedelta(days=1))

    booked = await reporter(session_factory).run_once()

    assert booked[salon] == (45 + 30) * 60
    assert booked[other_salon] == 45 * 60
    assert gauge_of(salon) == (45 + 30) * 60


async def test_what_is_not_an_open_booking_of_the_week_is_left_out(
    session_factory: async_sessionmaker[AsyncSession], make_booking: BookingFactory
) -> None:
    salon = SalonId(uuid4())
    master = uuid4()
    await make_booking(
        master_id=master,
        salon_id=salon,
        start_at=NOW + timedelta(hours=1),
        status="cancelled_by_client",
    )
    await make_booking(master_id=master, salon_id=salon, start_at=NOW - timedelta(hours=2))
    await make_booking(master_id=master, salon_id=salon, start_at=NOW + timedelta(days=8))

    booked = await reporter(session_factory).run_once()

    assert salon not in booked
    assert gauge_of(salon) is None


async def test_a_salon_whose_week_emptied_loses_its_series(
    session_factory: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    make_booking: BookingFactory,
) -> None:
    salon = uuid4()
    booking = await make_booking(
        master_id=uuid4(), salon_id=salon, start_at=NOW + timedelta(hours=3)
    )
    worker = reporter(session_factory)
    await worker.run_once()
    assert gauge_of(salon) == 45 * 60

    booking.status = "cancelled_by_salon"
    await session.commit()
    await worker.run_once()

    assert gauge_of(salon) is None
