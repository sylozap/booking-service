"""Unfolding a weekly schedule into the working intervals of one date."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from barber_booking.domain.errors import (
    ConflictingExceptions,
    DateInThePast,
    OverlappingWorkingHours,
)
from barber_booking.domain.schedule import (
    ExceptionKind,
    ScheduleException,
    TemplateInterval,
    TimeWindow,
    local_to_utc,
    validate_day_exceptions,
    validate_not_in_past,
    validate_weekly_template,
    working_intervals,
)
from barber_booking.domain.time_range import TimeRange

MOSCOW = ZoneInfo("Europe/Moscow")
BERLIN = ZoneInfo("Europe/Berlin")
YEKATERINBURG = ZoneInfo("Asia/Yekaterinburg")

MONDAY = date(2026, 10, 5)
SINCE = date(2026, 9, 1)


def utc(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def window(start: time, end: time) -> TimeWindow:
    return TimeWindow(start=start, end=end)


def template(
    day: date,
    start: time,
    end: time,
    *,
    valid_from: date = SINCE,
    valid_to: date | None = None,
) -> TemplateInterval:
    return TemplateInterval(
        weekday=day.weekday(),
        window=window(start, end),
        valid_from=valid_from,
        valid_to=valid_to,
    )


def exception(
    day: date, kind: ExceptionKind, start: time | None = None, end: time | None = None
) -> ScheduleException:
    hours = window(start, end) if start is not None and end is not None else None
    return ScheduleException(effective_on=day, kind=kind, window=hours)


# --- one ordinary day -------------------------------------------------------


def test_an_ordinary_day_follows_the_template() -> None:
    intervals = working_intervals(MONDAY, [template(MONDAY, time(10), time(20))], [], MOSCOW)

    assert intervals == [TimeRange(utc(MONDAY, 7), utc(MONDAY, 17))]


def test_a_day_without_a_template_line_has_no_work() -> None:
    tuesday_only = template(date(2026, 10, 6), time(10), time(20))

    assert working_intervals(MONDAY, [tuesday_only], [], MOSCOW) == []


def test_a_day_may_have_several_intervals() -> None:
    lines = [template(MONDAY, time(15), time(20)), template(MONDAY, time(10), time(14))]

    intervals = working_intervals(MONDAY, lines, [], MOSCOW)

    assert intervals == [
        TimeRange(utc(MONDAY, 7), utc(MONDAY, 11)),
        TimeRange(utc(MONDAY, 12), utc(MONDAY, 17)),
    ]


# --- exceptions -------------------------------------------------------------


def test_intervals_that_touch_become_one_shift() -> None:
    lines = [template(MONDAY, time(10), time(14)), template(MONDAY, time(14), time(18))]

    intervals = working_intervals(MONDAY, lines, [], MOSCOW)

    assert intervals == [TimeRange(utc(MONDAY, 7), utc(MONDAY, 15))]


def test_a_day_off_leaves_no_work() -> None:
    intervals = working_intervals(
        MONDAY,
        [template(MONDAY, time(10), time(20))],
        [exception(MONDAY, ExceptionKind.DAY_OFF)],
        MOSCOW,
    )

    assert intervals == []


def test_custom_hours_replace_the_template() -> None:
    intervals = working_intervals(
        MONDAY,
        [template(MONDAY, time(10), time(20))],
        [exception(MONDAY, ExceptionKind.CUSTOM_HOURS, time(12), time(16))],
        MOSCOW,
    )

    assert intervals == [TimeRange(utc(MONDAY, 9), utc(MONDAY, 13))]


def test_custom_hours_give_work_on_a_day_the_template_leaves_free() -> None:
    sunday = date(2026, 10, 11)

    intervals = working_intervals(
        sunday,
        [template(MONDAY, time(10), time(20))],
        [exception(sunday, ExceptionKind.CUSTOM_HOURS, time(11), time(15))],
        MOSCOW,
    )

    assert intervals == [TimeRange(utc(sunday, 8), utc(sunday, 12))]


def test_a_break_cuts_the_interval_in_two() -> None:
    intervals = working_intervals(
        MONDAY,
        [template(MONDAY, time(10), time(20))],
        [exception(MONDAY, ExceptionKind.BREAK, time(13), time(14))],
        MOSCOW,
    )

    assert intervals == [
        TimeRange(utc(MONDAY, 7), utc(MONDAY, 10)),
        TimeRange(utc(MONDAY, 11), utc(MONDAY, 17)),
    ]


def test_a_break_applies_to_custom_hours_too() -> None:
    intervals = working_intervals(
        MONDAY,
        [template(MONDAY, time(10), time(20))],
        [
            exception(MONDAY, ExceptionKind.CUSTOM_HOURS, time(12), time(18)),
            exception(MONDAY, ExceptionKind.BREAK, time(12), time(13)),
        ],
        MOSCOW,
    )

    assert intervals == [TimeRange(utc(MONDAY, 10), utc(MONDAY, 15))]


def test_an_exception_of_another_date_changes_nothing() -> None:
    intervals = working_intervals(
        MONDAY,
        [template(MONDAY, time(10), time(20))],
        [exception(date(2026, 10, 6), ExceptionKind.DAY_OFF)],
        MOSCOW,
    )

    assert intervals == [TimeRange(utc(MONDAY, 7), utc(MONDAY, 17))]


# --- versions of the template ------------------------------------------------


def test_of_two_templates_the_one_in_force_is_chosen() -> None:
    old = template(MONDAY, time(10), time(20), valid_from=SINCE, valid_to=date(2026, 9, 30))
    new = template(MONDAY, time(12), time(22), valid_from=date(2026, 10, 1))

    intervals = working_intervals(MONDAY, [old, new], [], MOSCOW)

    assert intervals == [TimeRange(utc(MONDAY, 9), utc(MONDAY, 19))]


def test_the_older_template_still_holds_before_the_new_one_starts() -> None:
    old = template(MONDAY, time(10), time(20), valid_from=SINCE, valid_to=date(2026, 10, 11))
    new = template(MONDAY, time(12), time(22), valid_from=date(2026, 10, 12))

    intervals = working_intervals(MONDAY, [old, new], [], MOSCOW)

    assert intervals == [TimeRange(utc(MONDAY, 7), utc(MONDAY, 17))]


def test_of_two_open_templates_the_later_version_wins() -> None:
    old = template(MONDAY, time(10), time(20), valid_from=SINCE)
    new = template(MONDAY, time(12), time(22), valid_from=date(2026, 10, 1))

    intervals = working_intervals(MONDAY, [old, new], [], MOSCOW)

    assert intervals == [TimeRange(utc(MONDAY, 9), utc(MONDAY, 19))]


# --- time zones and daylight saving ------------------------------------------


def test_a_salon_in_yekaterinburg_works_in_its_own_hours() -> None:
    intervals = working_intervals(MONDAY, [template(MONDAY, time(10), time(20))], [], YEKATERINBURG)

    assert intervals == [TimeRange(utc(MONDAY, 5), utc(MONDAY, 15))]


def test_the_night_the_clocks_go_forward_is_an_hour_shorter() -> None:
    spring = date(2026, 3, 29)

    line = template(spring, time(1), time(4), valid_from=date(2026, 1, 1))

    intervals = working_intervals(spring, [line], [], BERLIN)

    # 01:00 CET to 04:00 CEST: two hours of real time, not three.
    assert intervals == [TimeRange(utc(spring, 0), utc(spring, 2))]


def test_the_night_the_clocks_go_back_is_an_hour_longer() -> None:
    autumn = date(2026, 10, 25)

    intervals = working_intervals(autumn, [template(autumn, time(1), time(4))], [], BERLIN)

    # 01:00 CEST to 04:00 CET: four hours of real time.
    assert intervals == [TimeRange(utc(date(2026, 10, 24), 23), utc(autumn, 3))]


def test_a_local_time_in_the_gap_reads_as_the_offset_before_it() -> None:
    # PostgreSQL does the same with AT TIME ZONE, and the SQL of availability
    # has to agree with this module.
    assert local_to_utc(date(2026, 3, 29), time(2, 30), BERLIN) == utc(date(2026, 3, 29), 1, 30)


def test_a_repeated_local_time_reads_as_its_later_occurrence() -> None:
    assert local_to_utc(date(2026, 10, 25), time(2, 30), BERLIN) == utc(date(2026, 10, 25), 1, 30)


# --- validation -------------------------------------------------------------


def test_overlapping_intervals_of_one_weekday_are_refused() -> None:
    lines = [template(MONDAY, time(10), time(14)), template(MONDAY, time(13), time(18))]

    with pytest.raises(OverlappingWorkingHours):
        validate_weekly_template(lines)


def test_intervals_that_only_touch_are_accepted() -> None:
    lines = [template(MONDAY, time(10), time(14)), template(MONDAY, time(14), time(18))]

    validate_weekly_template(lines)


def test_the_same_hours_on_two_weekdays_are_accepted() -> None:
    lines = [
        template(MONDAY, time(10), time(20)),
        template(date(2026, 10, 6), time(10), time(20)),
    ]

    validate_weekly_template(lines)


def test_a_day_off_does_not_share_its_date() -> None:
    with pytest.raises(ConflictingExceptions):
        validate_day_exceptions(
            [
                exception(MONDAY, ExceptionKind.DAY_OFF),
                exception(MONDAY, ExceptionKind.BREAK, time(13), time(14)),
            ]
        )


def test_overlapping_custom_hours_are_refused() -> None:
    with pytest.raises(ConflictingExceptions):
        validate_day_exceptions(
            [
                exception(MONDAY, ExceptionKind.CUSTOM_HOURS, time(10), time(14)),
                exception(MONDAY, ExceptionKind.CUSTOM_HOURS, time(13), time(18)),
            ]
        )


def test_overlapping_breaks_are_refused() -> None:
    with pytest.raises(ConflictingExceptions):
        validate_day_exceptions(
            [
                exception(MONDAY, ExceptionKind.BREAK, time(13), time(14)),
                exception(MONDAY, ExceptionKind.BREAK, time(13, 30), time(15)),
            ]
        )


def test_custom_hours_and_breaks_may_share_a_date() -> None:
    validate_day_exceptions(
        [
            exception(MONDAY, ExceptionKind.CUSTOM_HOURS, time(10), time(14)),
            exception(MONDAY, ExceptionKind.CUSTOM_HOURS, time(16), time(20)),
            exception(MONDAY, ExceptionKind.BREAK, time(12), time(12, 30)),
        ]
    )


def test_a_change_for_yesterday_is_refused() -> None:
    with pytest.raises(DateInThePast):
        validate_not_in_past(date(2026, 10, 4), today=MONDAY)


def test_a_change_for_today_is_accepted() -> None:
    validate_not_in_past(MONDAY, today=MONDAY)


def test_a_window_that_ends_before_it_starts_cannot_exist() -> None:
    with pytest.raises(ValueError, match="end after it starts"):
        TimeWindow(start=time(20), end=time(10))


def test_a_day_off_cannot_carry_hours() -> None:
    with pytest.raises(ValueError, match="day off"):
        ScheduleException(
            effective_on=MONDAY,
            kind=ExceptionKind.DAY_OFF,
            window=window(time(10), time(12)),
        )


def test_hours_or_a_break_cannot_come_without_times() -> None:
    with pytest.raises(ValueError, match="needs its hours"):
        ScheduleException(effective_on=MONDAY, kind=ExceptionKind.BREAK)
