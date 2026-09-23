"""The query and the domain answer the same thing.

Slot arithmetic exists twice: once in ``domain/slots.py``, which is the
reference and is covered by fast tests, and once in SQL, which serves the hot
path. This runs both over the same cases and requires the same starts. A change
to one without the other is a defect, and this is what says so.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from barber_booking.domain.identifiers import MasterId
from barber_booking.domain.schedule import (
    ExceptionKind,
    ScheduleException,
    TemplateInterval,
    TimeWindow,
    working_intervals,
)
from barber_booking.domain.slots import slot_starts
from barber_booking.domain.time_range import TimeRange
from barber_booking.models.booking import Booking, occupied_range_of
from barber_booking.models.master_settings import MasterSettings
from barber_booking.repositories.availability import AvailabilityRepository

pytestmark = pytest.mark.integration

MasterSettingsFactory = Callable[..., Awaitable[MasterSettings]]
BookingFactory = Callable[..., Awaitable[Booking]]
TemplateFactory = Callable[..., Awaitable[object]]
ExceptionFactory = Callable[..., Awaitable[object]]

MONDAY = date(2026, 10, 5)
SPRING_FORWARD = date(2026, 3, 29)
FALL_BACK = date(2026, 10, 25)

LONG_AGO = datetime(2020, 1, 1, tzinfo=UTC)
FAR_AHEAD = datetime(2030, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Hours:
    """One interval of a template or of an exception, in local time."""

    start: time
    end: time


@dataclass(frozen=True, slots=True)
class BookedTime:
    """A booking, in the terms both sides understand."""

    start_at: datetime
    duration_min: int
    buffer_min: int = 0


@dataclass(frozen=True, slots=True)
class Case:
    """One situation, laid out for both implementations."""

    name: str
    day: date
    timezone: str = "Europe/Moscow"
    template: Sequence[Hours] = ()
    custom_hours: Sequence[Hours] = ()
    breaks: Sequence[Hours] = ()
    day_off: bool = False
    bookings: Sequence[BookedTime] = field(default_factory=tuple)
    duration_min: int = 45
    buffer_min: int = 0
    step_min: int = 15


CASES = [
    Case(name="a plain day", day=MONDAY, template=[Hours(time(10), time(19))]),
    Case(
        name="two intervals that touch",
        day=MONDAY,
        template=[Hours(time(10), time(14)), Hours(time(14), time(18))],
    ),
    Case(
        name="a split shift",
        day=MONDAY,
        template=[Hours(time(10), time(13)), Hours(time(16), time(20))],
    ),
    Case(
        name="a break in the middle",
        day=MONDAY,
        template=[Hours(time(9), time(18))],
        breaks=[Hours(time(13), time(13, 40))],
    ),
    Case(
        name="two breaks",
        day=MONDAY,
        template=[Hours(time(9), time(18))],
        breaks=[Hours(time(11), time(11, 20)), Hours(time(15), time(16))],
    ),
    Case(
        name="custom hours instead of the template",
        day=MONDAY,
        template=[Hours(time(10), time(20))],
        custom_hours=[Hours(time(12, 10), time(16))],
    ),
    Case(name="a day off", day=MONDAY, template=[Hours(time(10), time(20))], day_off=True),
    Case(
        name="bookings with buffers",
        day=MONDAY,
        template=[Hours(time(9), time(18))],
        bookings=[
            BookedTime(datetime(2026, 10, 5, 7, 0, tzinfo=UTC), 45, 15),
            BookedTime(datetime(2026, 10, 5, 11, 30, tzinfo=UTC), 90, 0),
        ],
        buffer_min=15,
    ),
    Case(
        name="a service longer than the gap between bookings",
        day=MONDAY,
        template=[Hours(time(9), time(14))],
        bookings=[BookedTime(datetime(2026, 10, 5, 7, 30, tzinfo=UTC), 60, 0)],
        duration_min=90,
        step_min=30,
    ),
    Case(
        name="another time zone",
        day=MONDAY,
        timezone="Asia/Yekaterinburg",
        template=[Hours(time(10), time(16))],
        breaks=[Hours(time(12), time(13))],
    ),
    Case(
        name="the day the clocks go forward",
        day=SPRING_FORWARD,
        timezone="Europe/Berlin",
        template=[Hours(time(0, 30), time(6))],
        duration_min=30,
        step_min=30,
    ),
    Case(
        name="the day the clocks go back",
        day=FALL_BACK,
        timezone="Europe/Berlin",
        template=[Hours(time(0, 30), time(6))],
        breaks=[Hours(time(3), time(3, 30))],
        duration_min=30,
        step_min=30,
    ),
]


def domain_starts(case: Case) -> list[datetime]:
    """What the reference implementation offers."""
    zone = ZoneInfo(case.timezone)
    templates = [
        TemplateInterval(
            weekday=case.day.weekday(),
            window=TimeWindow(start=hours.start, end=hours.end),
            valid_from=date(2020, 1, 1),
        )
        for hours in case.template
    ]
    exceptions = [
        ScheduleException(
            effective_on=case.day,
            kind=ExceptionKind.CUSTOM_HOURS,
            window=TimeWindow(start=hours.start, end=hours.end),
        )
        for hours in case.custom_hours
    ] + [
        ScheduleException(
            effective_on=case.day,
            kind=ExceptionKind.BREAK,
            window=TimeWindow(start=hours.start, end=hours.end),
        )
        for hours in case.breaks
    ]
    if case.day_off:
        exceptions.append(
            ScheduleException(effective_on=case.day, kind=ExceptionKind.DAY_OFF, window=None)
        )

    busy = [
        TimeRange(
            booking.start_at,
            booking.start_at + timedelta(minutes=booking.duration_min + booking.buffer_min),
        )
        for booking in case.bookings
    ]
    return slot_starts(
        work=working_intervals(case.day, templates, exceptions, zone),
        busy=busy,
        duration_min=case.duration_min,
        buffer_min=case.buffer_min,
        step_min=case.step_min,
    )


async def lay_out(
    case: Case,
    *,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
    make_exception: ExceptionFactory,
    session: AsyncSession,
) -> MasterSettings:
    """Write the case into the database the query reads."""
    master = await make_master_settings(timezone=case.timezone)
    for hours in case.template:
        await make_template(
            master_id=master.master_id,
            weekday=case.day.weekday(),
            start_time=hours.start,
            end_time=hours.end,
        )
    for hours in case.custom_hours:
        await make_exception(
            master_id=master.master_id,
            effective_on=case.day,
            kind="custom_hours",
            start_time=hours.start,
            end_time=hours.end,
        )
    for hours in case.breaks:
        await make_exception(
            master_id=master.master_id,
            effective_on=case.day,
            kind="break",
            start_time=hours.start,
            end_time=hours.end,
        )
    if case.day_off:
        await make_exception(master_id=master.master_id, effective_on=case.day, kind="day_off")

    for booking in case.bookings:
        end_at = booking.start_at + timedelta(minutes=booking.duration_min)
        session.add(
            Booking(
                salon_id=master.salon_id,
                master_id=master.master_id,
                client_user_id=master.user_id,
                service_id=master.master_id,
                service_name="Haircut",
                price=Decimal("3500.00"),
                currency="RUB",
                duration_min=booking.duration_min,
                buffer_min=booking.buffer_min,
                start_at=booking.start_at,
                end_at=end_at,
                status="confirmed",
                created_by=master.user_id,
            )
        )
    await session.flush()
    return master


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
async def test_the_query_and_the_domain_offer_the_same_starts(
    case: Case,
    session: AsyncSession,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
    make_exception: ExceptionFactory,
) -> None:
    master = await lay_out(
        case,
        make_master_settings=make_master_settings,
        make_template=make_template,
        make_exception=make_exception,
        session=session,
    )

    from_database = await AvailabilityRepository(session).slots(
        master_id=MasterId(master.master_id),
        date_from=case.day,
        date_to=case.day,
        timezone=case.timezone,
        duration_min=case.duration_min,
        buffer_min=case.buffer_min,
        step_min=case.step_min,
        not_before=LONG_AGO,
        not_after=FAR_AHEAD,
    )

    assert from_database.get(case.day, []) == domain_starts(case)


def test_the_occupied_range_of_a_booking_is_what_the_domain_calls_busy() -> None:
    start_at = datetime(2026, 10, 5, 7, 0, tzinfo=UTC)
    end_at = start_at + timedelta(minutes=45)

    occupied = occupied_range_of(start_at, end_at, 15)

    assert (occupied.lower, occupied.upper) == (start_at, end_at + timedelta(minutes=15))
