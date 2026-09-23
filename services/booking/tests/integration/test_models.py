"""Database constraints of the booking schema, on a real PostgreSQL."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from barber_booking.models.booking import Booking
from barber_booking.models.master_settings import MasterSettings
from barber_booking.models.schedule_exception import ScheduleException
from barber_booking.models.schedule_template import ScheduleTemplate
from barber_common.db.errors import (
    SQLSTATE_CHECK_VIOLATION,
    SQLSTATE_EXCLUSION_VIOLATION,
    sqlstate_of,
)

pytestmark = pytest.mark.integration

MasterSettingsFactory = Callable[..., Awaitable[MasterSettings]]
BookingFactory = Callable[..., Awaitable[Booking]]

TEN_O_CLOCK = datetime(2026, 10, 5, 7, 0, tzinfo=UTC)


async def flush_error(session: AsyncSession) -> str | None:
    with pytest.raises(IntegrityError) as failure:
        await session.flush()
    return sqlstate_of(failure.value)


async def test_a_booking_that_does_not_end_after_it_starts_is_refused(
    session: AsyncSession,
) -> None:
    client_id = uuid4()
    # Zero length with a buffer: the range itself is valid, so only the check
    # on the times can refuse it.
    session.add(
        Booking(
            salon_id=uuid4(),
            master_id=uuid4(),
            client_user_id=client_id,
            service_id=uuid4(),
            service_name="Haircut",
            price=Decimal("3500.00"),
            currency="RUB",
            duration_min=45,
            buffer_min=15,
            start_at=TEN_O_CLOCK,
            end_at=TEN_O_CLOCK,
            status="confirmed",
            created_by=client_id,
        )
    )

    assert await flush_error(session) == SQLSTATE_CHECK_VIOLATION


async def test_an_unknown_status_is_refused(
    session: AsyncSession, make_booking: BookingFactory
) -> None:
    with pytest.raises(IntegrityError) as failure:
        await make_booking(master_id=uuid4(), start_at=TEN_O_CLOCK, status="maybe")

    assert sqlstate_of(failure.value) == SQLSTATE_CHECK_VIOLATION


async def test_an_overlap_of_two_active_bookings_is_refused(
    make_booking: BookingFactory,
) -> None:
    master_id = uuid4()
    await make_booking(master_id=master_id, start_at=TEN_O_CLOCK)

    with pytest.raises(IntegrityError) as failure:
        await make_booking(master_id=master_id, start_at=TEN_O_CLOCK + timedelta(minutes=30))

    assert sqlstate_of(failure.value) == SQLSTATE_EXCLUSION_VIOLATION


async def test_the_buffer_counts_towards_the_overlap(make_booking: BookingFactory) -> None:
    master_id = uuid4()
    await make_booking(master_id=master_id, start_at=TEN_O_CLOCK, buffer_min=15)

    with pytest.raises(IntegrityError) as failure:
        await make_booking(master_id=master_id, start_at=TEN_O_CLOCK + timedelta(minutes=45))

    assert sqlstate_of(failure.value) == SQLSTATE_EXCLUSION_VIOLATION


async def test_a_booking_may_start_exactly_where_the_buffer_ends(
    make_booking: BookingFactory,
) -> None:
    master_id = uuid4()
    await make_booking(master_id=master_id, start_at=TEN_O_CLOCK, buffer_min=15)

    booking = await make_booking(master_id=master_id, start_at=TEN_O_CLOCK + timedelta(hours=1))

    assert booking.start_at == TEN_O_CLOCK + timedelta(hours=1)


async def test_two_masters_may_be_busy_at_the_same_time(make_booking: BookingFactory) -> None:
    await make_booking(master_id=uuid4(), start_at=TEN_O_CLOCK)

    booking = await make_booking(master_id=uuid4(), start_at=TEN_O_CLOCK)

    assert booking.id is not None


async def test_a_cancelled_booking_does_not_block_the_time(
    session: AsyncSession, make_booking: BookingFactory
) -> None:
    master_id = uuid4()
    cancelled = await make_booking(master_id=master_id, start_at=TEN_O_CLOCK)
    await session.execute(
        update(Booking).where(Booking.id == cancelled.id).values(status="cancelled_by_client")
    )

    booking = await make_booking(master_id=master_id, start_at=TEN_O_CLOCK)

    assert booking.status == "confirmed"


async def test_a_range_that_disagrees_with_the_times_is_refused(
    session: AsyncSession, make_booking: BookingFactory
) -> None:
    booking = await make_booking(master_id=uuid4(), start_at=TEN_O_CLOCK, buffer_min=15)

    with pytest.raises(IntegrityError) as failure:
        # Moving the start without the range: the mistake a second write path
        # would make.
        await session.execute(
            update(Booking)
            .where(Booking.id == booking.id)
            .values(start_at=TEN_O_CLOCK - timedelta(hours=1))
        )

    assert sqlstate_of(failure.value) == SQLSTATE_CHECK_VIOLATION


async def test_a_negative_buffer_of_a_master_is_refused(
    session: AsyncSession,
) -> None:
    session.add(
        MasterSettings(
            master_id=uuid4(),
            salon_id=uuid4(),
            user_id=uuid4(),
            timezone="Europe/Moscow",
            buffer_after_min=-5,
        )
    )

    assert await flush_error(session) == SQLSTATE_CHECK_VIOLATION


def test_an_unknown_time_zone_is_refused_by_the_model() -> None:
    with pytest.raises(ValueError, match="not a known IANA time zone"):
        MasterSettings(master_id=uuid4(), salon_id=uuid4(), user_id=uuid4(), timezone="Mars/Base")


async def test_a_template_interval_that_ends_before_it_starts_is_refused(
    session: AsyncSession, make_master_settings: MasterSettingsFactory
) -> None:
    master = await make_master_settings()
    session.add(
        ScheduleTemplate(
            master_id=master.master_id,
            weekday=0,
            start_time=time(20),
            end_time=time(10),
            valid_from=date(2026, 10, 1),
        )
    )

    assert await flush_error(session) == SQLSTATE_CHECK_VIOLATION


async def test_a_day_off_with_hours_is_refused(
    session: AsyncSession, make_master_settings: MasterSettingsFactory
) -> None:
    master = await make_master_settings()
    session.add(
        ScheduleException(
            master_id=master.master_id,
            effective_on=date(2026, 10, 5),
            kind="day_off",
            start_time=time(10),
            end_time=time(12),
        )
    )

    assert await flush_error(session) == SQLSTATE_CHECK_VIOLATION


async def test_a_break_without_hours_is_refused(
    session: AsyncSession, make_master_settings: MasterSettingsFactory
) -> None:
    master = await make_master_settings()
    session.add(
        ScheduleException(master_id=master.master_id, effective_on=date(2026, 10, 5), kind="break")
    )

    assert await flush_error(session) == SQLSTATE_CHECK_VIOLATION


async def test_the_overlap_constraint_is_a_partial_gist_exclusion(session: AsyncSession) -> None:
    result = await session.execute(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ex_bookings_no_overlapping'"
        )
    )
    definition = result.scalar_one()

    assert "EXCLUDE USING gist" in definition
    assert "cancelled" not in definition
    for status in ("pending", "confirmed", "completed", "no_show"):
        assert status in definition
