"""The availability query against a real PostgreSQL.

Every case is a rule of the domain seen from the database side: a schedule
unfolded into instants, breaks cut out of it, a grid laid over what is left,
and bookings taking their time out of the answer.
"""

from __future__ import annotations

import time as clock
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from barber_booking.domain.identifiers import MasterId
from barber_booking.models.booking import Booking
from barber_booking.models.master_settings import MasterSettings
from barber_booking.repositories.availability import AvailabilityRepository

pytestmark = pytest.mark.integration

MasterSettingsFactory = Callable[..., Awaitable[MasterSettings]]
BookingFactory = Callable[..., Awaitable[Booking]]
TemplateFactory = Callable[..., Awaitable[object]]
ExceptionFactory = Callable[..., Awaitable[object]]

MOSCOW = "Europe/Moscow"
YEKATERINBURG = "Asia/Yekaterinburg"
BERLIN = "Europe/Berlin"

# A Monday, and the same day in the salon's local time: 10:00 in Moscow is
# 07:00 UTC.
MONDAY = date(2026, 10, 5)
# Well before any date under test, so nothing is cut off by the lead time.
LONG_AGO = datetime(2020, 1, 1, tzinfo=UTC)
FAR_AHEAD = datetime(2030, 1, 1, tzinfo=UTC)


def utc(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


class Availability:
    """The query under test, with the parameters a test does not care about."""

    def __init__(self, session: AsyncSession) -> None:
        self._repository = AvailabilityRepository(session)

    async def of(
        self,
        master: MasterSettings,
        *,
        day: date = MONDAY,
        until: date | None = None,
        timezone: str = MOSCOW,
        duration_min: int = 45,
        buffer_min: int = 0,
        step_min: int = 15,
        not_before: datetime = LONG_AGO,
        not_after: datetime = FAR_AHEAD,
    ) -> dict[date, list[datetime]]:
        return await self._repository.slots(
            master_id=MasterId(master.master_id),
            date_from=day,
            date_to=until or day,
            timezone=timezone,
            duration_min=duration_min,
            buffer_min=buffer_min,
            step_min=step_min,
            not_before=not_before,
            not_after=not_after,
        )


@pytest.fixture
def availability(session: AsyncSession) -> Availability:
    return Availability(session)


@pytest.fixture
async def master(make_master_settings: MasterSettingsFactory) -> MasterSettings:
    return await make_master_settings()


# --- the schedule -----------------------------------------------------------


async def test_a_day_without_a_template_has_no_slots(
    availability: Availability, master: MasterSettings
) -> None:
    assert await availability.of(master) == {}


async def test_a_day_by_the_template_is_cut_into_the_grid(
    availability: Availability, master: MasterSettings, make_template: TemplateFactory
) -> None:
    await make_template(
        master_id=master.master_id, weekday=0, start_time=time(10), end_time=time(12)
    )

    slots = await availability.of(master)

    # 10:00 to 11:15 in the salon: the last start whose 45 minutes still fit.
    assert slots == {
        MONDAY: [
            utc(MONDAY, 7),
            utc(MONDAY, 7, 15),
            utc(MONDAY, 7, 30),
            utc(MONDAY, 7, 45),
            utc(MONDAY, 8),
            utc(MONDAY, 8, 15),
        ]
    }


async def test_a_slot_that_does_not_fit_before_the_end_is_not_offered(
    availability: Availability, master: MasterSettings, make_template: TemplateFactory
) -> None:
    await make_template(
        master_id=master.master_id, weekday=0, start_time=time(10), end_time=time(11)
    )

    slots = await availability.of(master, duration_min=50)

    assert slots == {MONDAY: [utc(MONDAY, 7)]}


async def test_custom_hours_replace_the_template(
    availability: Availability,
    master: MasterSettings,
    make_template: TemplateFactory,
    make_exception: ExceptionFactory,
) -> None:
    await make_template(
        master_id=master.master_id, weekday=0, start_time=time(10), end_time=time(20)
    )
    await make_exception(
        master_id=master.master_id,
        effective_on=MONDAY,
        kind="custom_hours",
        start_time=time(12),
        end_time=time(13),
    )

    slots = await availability.of(master)

    assert slots == {MONDAY: [utc(MONDAY, 9), utc(MONDAY, 9, 15)]}


async def test_a_day_off_leaves_nothing(
    availability: Availability,
    master: MasterSettings,
    make_template: TemplateFactory,
    make_exception: ExceptionFactory,
) -> None:
    await make_template(
        master_id=master.master_id, weekday=0, start_time=time(10), end_time=time(20)
    )
    await make_exception(master_id=master.master_id, effective_on=MONDAY, kind="day_off")

    assert await availability.of(master) == {}


async def test_a_break_cuts_the_day_in_two(
    availability: Availability,
    master: MasterSettings,
    make_template: TemplateFactory,
    make_exception: ExceptionFactory,
) -> None:
    await make_template(
        master_id=master.master_id, weekday=0, start_time=time(10), end_time=time(13)
    )
    await make_exception(
        master_id=master.master_id,
        effective_on=MONDAY,
        kind="break",
        start_time=time(11),
        end_time=time(12),
    )

    slots = await availability.of(master, duration_min=60, step_min=60)

    # 10:00 before the break and 12:00 after it; 11:00 is the break itself.
    assert slots == {MONDAY: [utc(MONDAY, 7), utc(MONDAY, 9)]}


async def test_of_two_template_versions_the_later_one_is_used(
    availability: Availability, master: MasterSettings, make_template: TemplateFactory
) -> None:
    await make_template(
        master_id=master.master_id,
        weekday=0,
        start_time=time(10),
        end_time=time(20),
        valid_from=date(2020, 1, 1),
    )
    await make_template(
        master_id=master.master_id,
        weekday=0,
        start_time=time(12),
        end_time=time(13),
        valid_from=date(2026, 9, 1),
    )

    slots = await availability.of(master)

    assert slots == {MONDAY: [utc(MONDAY, 9), utc(MONDAY, 9, 15)]}


# --- bookings taking their time ---------------------------------------------


async def test_a_booking_takes_its_time_out_of_the_answer(
    availability: Availability,
    master: MasterSettings,
    make_template: TemplateFactory,
    make_booking: BookingFactory,
) -> None:
    await make_template(
        master_id=master.master_id, weekday=0, start_time=time(10), end_time=time(13)
    )
    await make_booking(master_id=master.master_id, start_at=utc(MONDAY, 8), duration_min=60)

    slots = await availability.of(master, duration_min=60, step_min=60)

    assert slots == {MONDAY: [utc(MONDAY, 7), utc(MONDAY, 9)]}


async def test_the_buffer_of_a_booking_closes_the_next_start(
    availability: Availability,
    master: MasterSettings,
    make_template: TemplateFactory,
    make_booking: BookingFactory,
) -> None:
    await make_template(
        master_id=master.master_id, weekday=0, start_time=time(10), end_time=time(14)
    )
    # 10:00-10:45 with a buffer of 15 occupies the master until 11:00.
    await make_booking(
        master_id=master.master_id, start_at=utc(MONDAY, 7), duration_min=45, buffer_min=15
    )

    slots = await availability.of(master, buffer_min=15)

    assert utc(MONDAY, 7, 45) not in slots[MONDAY]
    assert utc(MONDAY, 8) in slots[MONDAY]


async def test_a_cancelled_booking_gives_its_time_back(
    availability: Availability,
    master: MasterSettings,
    make_template: TemplateFactory,
    make_booking: BookingFactory,
) -> None:
    await make_template(
        master_id=master.master_id, weekday=0, start_time=time(10), end_time=time(11)
    )
    await make_booking(
        master_id=master.master_id,
        start_at=utc(MONDAY, 7),
        duration_min=60,
        status="cancelled_by_client",
    )

    slots = await availability.of(master, duration_min=60)

    assert slots == {MONDAY: [utc(MONDAY, 7)]}


async def test_the_booking_of_another_master_changes_nothing(
    availability: Availability,
    master: MasterSettings,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
    make_booking: BookingFactory,
) -> None:
    other = await make_master_settings()
    await make_template(
        master_id=master.master_id, weekday=0, start_time=time(10), end_time=time(11)
    )
    await make_booking(master_id=other.master_id, start_at=utc(MONDAY, 7), duration_min=60)

    slots = await availability.of(master, duration_min=60)

    assert slots == {MONDAY: [utc(MONDAY, 7)]}


# --- windows ----------------------------------------------------------------


async def test_starts_before_the_lead_time_are_cut_off(
    availability: Availability, master: MasterSettings, make_template: TemplateFactory
) -> None:
    await make_template(
        master_id=master.master_id, weekday=0, start_time=time(10), end_time=time(12)
    )

    slots = await availability.of(master, not_before=utc(MONDAY, 7, 20))

    assert slots == {
        MONDAY: [utc(MONDAY, 7, 30), utc(MONDAY, 7, 45), utc(MONDAY, 8), utc(MONDAY, 8, 15)]
    }


async def test_starts_beyond_the_horizon_are_cut_off(
    availability: Availability, master: MasterSettings, make_template: TemplateFactory
) -> None:
    await make_template(
        master_id=master.master_id, weekday=0, start_time=time(10), end_time=time(12)
    )

    slots = await availability.of(master, not_after=utc(MONDAY, 7, 16))

    assert slots == {MONDAY: [utc(MONDAY, 7), utc(MONDAY, 7, 15)]}


async def test_a_range_of_dates_answers_day_by_day(
    availability: Availability, master: MasterSettings, make_template: TemplateFactory
) -> None:
    await make_template(
        master_id=master.master_id, weekday=0, start_time=time(10), end_time=time(11)
    )
    await make_template(
        master_id=master.master_id, weekday=2, start_time=time(10), end_time=time(11)
    )

    slots = await availability.of(master, day=MONDAY, until=MONDAY + timedelta(days=6))

    assert list(slots) == [MONDAY, MONDAY + timedelta(days=2)]


# --- time zones -------------------------------------------------------------


async def test_a_salon_in_yekaterinburg_works_in_its_own_hours(
    availability: Availability,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await make_master_settings(timezone=YEKATERINBURG)
    await make_template(
        master_id=master.master_id, weekday=0, start_time=time(10), end_time=time(11)
    )

    slots = await availability.of(master, timezone=YEKATERINBURG, duration_min=60)

    assert slots == {MONDAY: [utc(MONDAY, 5)]}


async def test_the_day_the_clocks_go_forward_is_an_hour_shorter(
    availability: Availability,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    spring = date(2026, 3, 29)
    master = await make_master_settings(timezone=BERLIN)
    await make_template(
        master_id=master.master_id,
        weekday=spring.weekday(),
        start_time=time(1),
        end_time=time(4),
    )

    slots = await availability.of(master, day=spring, timezone=BERLIN, duration_min=60, step_min=60)

    # 01:00 CET to 04:00 CEST is two hours of real time, so two starts.
    assert slots == {spring: [utc(spring, 0), utc(spring, 1)]}


async def test_the_day_the_clocks_go_back_is_an_hour_longer(
    availability: Availability,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    autumn = date(2026, 10, 25)
    master = await make_master_settings(timezone=BERLIN)
    await make_template(
        master_id=master.master_id,
        weekday=autumn.weekday(),
        start_time=time(1),
        end_time=time(4),
    )

    slots = await availability.of(master, day=autumn, timezone=BERLIN, duration_min=60, step_min=60)

    assert slots == {
        autumn: [
            utc(date(2026, 10, 24), 23),
            utc(autumn, 0),
            utc(autumn, 1),
            utc(autumn, 2),
        ]
    }


# --- what it costs ----------------------------------------------------------


async def test_a_day_with_twenty_bookings_stays_well_inside_the_budget(
    availability: Availability,
    master: MasterSettings,
    make_template: TemplateFactory,
    make_booking: BookingFactory,
) -> None:
    await make_template(
        master_id=master.master_id, weekday=0, start_time=time(8), end_time=time(22)
    )
    for index in range(20):
        await make_booking(
            master_id=master.master_id,
            start_at=utc(MONDAY, 5) + timedelta(minutes=30 * index),
            duration_min=20,
        )

    started = clock.monotonic()
    slots = await availability.of(master, duration_min=20)
    elapsed = clock.monotonic() - started

    assert slots[MONDAY]
    # The target of the design is 300 ms; the assertion is the SLO of one
    # availability request, so a loaded machine does not turn it into a flake.
    assert elapsed < 1.0
