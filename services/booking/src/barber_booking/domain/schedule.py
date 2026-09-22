"""A weekly schedule, its exceptions, and the working time of one date.

Everything is written in the salon's local time and unfolded here into real
instants. A local time that the clocks skip or repeat is read the way
PostgreSQL's ``AT TIME ZONE`` reads it, so this module and the availability
query agree on every date of the year.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from enum import StrEnum
from itertools import combinations
from zoneinfo import ZoneInfo

from barber_booking.domain.errors import (
    ConflictingExceptions,
    ExceptionInThePast,
    OverlappingWorkingHours,
)
from barber_booking.domain.time_range import TimeRange

__all__ = [
    "ExceptionKind",
    "ScheduleException",
    "TemplateInterval",
    "TimeWindow",
    "local_to_utc",
    "validate_day_exceptions",
    "validate_exception_date",
    "validate_weekly_template",
    "working_intervals",
]


class ExceptionKind(StrEnum):
    DAY_OFF = "day_off"
    CUSTOM_HOURS = "custom_hours"
    BREAK = "break"


@dataclass(frozen=True, slots=True)
class TimeWindow:
    """Wall-clock hours of one day, ``[start, end)``. Never across midnight."""

    start: time
    end: time

    def __post_init__(self) -> None:
        if self.start.tzinfo is not None or self.end.tzinfo is not None:
            raise ValueError("a window is local wall-clock time, without a zone")
        if self.end <= self.start:
            raise ValueError("a window has to end after it starts")

    def overlaps(self, other: TimeWindow) -> bool:
        return self.start < other.end and other.start < self.end


@dataclass(frozen=True, slots=True)
class TemplateInterval:
    """One line of the weekly template. ``weekday`` counts from Monday as 0."""

    weekday: int
    window: TimeWindow
    valid_from: date
    # Inclusive; ``None`` means until further notice.
    valid_to: date | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.weekday <= 6:
            raise ValueError("a weekday is a number from 0 to 6")
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("a template cannot end before it starts")

    def is_valid_on(self, day: date) -> bool:
        if day < self.valid_from:
            return False
        return self.valid_to is None or day <= self.valid_to


@dataclass(frozen=True, slots=True)
class ScheduleException:
    """A change to one date. A day off has no hours; the other two must."""

    effective_on: date
    kind: ExceptionKind
    window: TimeWindow | None = None

    def __post_init__(self) -> None:
        if self.kind is ExceptionKind.DAY_OFF and self.window is not None:
            raise ValueError("a day off has no hours")
        if self.kind is not ExceptionKind.DAY_OFF and self.window is None:
            raise ValueError(f"an exception of kind {self.kind} needs its hours")


def local_to_utc(day: date, at: time, zone: ZoneInfo) -> datetime:
    """The instant a wall-clock time of the salon names.

    A time the clocks skip is read with the offset in force before the jump,
    and a time they repeat as its later occurrence -- both as PostgreSQL does.
    """
    naive = datetime.combine(day, at)
    earlier = naive.replace(tzinfo=zone, fold=0)
    later = naive.replace(tzinfo=zone, fold=1)
    if earlier.utcoffset() == later.utcoffset():
        return earlier.astimezone(UTC)

    exists = earlier.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == naive
    return (later if exists else earlier).astimezone(UTC)


def working_intervals(
    day: date,
    templates: Sequence[TemplateInterval],
    exceptions: Sequence[ScheduleException],
    zone: ZoneInfo,
) -> list[TimeRange]:
    """The real time a master works on one date of the salon's calendar.

    A day off leaves nothing. Custom hours replace the template for the date.
    Breaks are cut out of whatever hours remain. Of the template lines in force
    on the date, the latest version -- the greatest ``valid_from`` -- wins.
    """
    todays = [exception for exception in exceptions if exception.effective_on == day]
    if any(exception.kind is ExceptionKind.DAY_OFF for exception in todays):
        return []

    windows = [
        exception.window
        for exception in todays
        if exception.kind is ExceptionKind.CUSTOM_HOURS and exception.window is not None
    ] or _template_windows(day, templates)

    intervals = [_unfold(day, window, zone) for window in windows]
    for exception in todays:
        if exception.kind is ExceptionKind.BREAK and exception.window is not None:
            pause = _unfold(day, exception.window, zone)
            intervals = [rest for interval in intervals for rest in interval.minus(pause)]
    return sorted(intervals)


def validate_weekly_template(lines: Sequence[TemplateInterval]) -> None:
    """Refuse a template where two intervals of one weekday overlap."""
    for first, second in combinations(lines, 2):
        if first.weekday == second.weekday and first.window.overlaps(second.window):
            raise OverlappingWorkingHours(
                f"Working hours of weekday {first.weekday} overlap each other"
            )


def validate_day_exceptions(exceptions: Sequence[ScheduleException]) -> None:
    """Refuse a set of exceptions of one date that contradict each other.

    A day off stands alone. Custom hours may be several, and so may breaks, as
    long as those of one kind do not overlap.
    """
    kinds = [exception.kind for exception in exceptions]
    if ExceptionKind.DAY_OFF in kinds and len(kinds) > 1:
        raise ConflictingExceptions("A day off cannot share its date with other exceptions")

    for first, second in combinations(exceptions, 2):
        if (
            first.kind is second.kind
            and first.window is not None
            and second.window is not None
            and first.window.overlaps(second.window)
        ):
            raise ConflictingExceptions(f"Two exceptions of kind {first.kind} overlap each other")


def validate_exception_date(effective_on: date, *, today: date) -> None:
    """Refuse an exception for a date that has already begun.

    ``today`` is the salon's current date: the past ends at local midnight, not
    at midnight in UTC.
    """
    if effective_on < today:
        raise ExceptionInThePast("An exception cannot be set for a date in the past")


def _template_windows(day: date, templates: Sequence[TemplateInterval]) -> list[TimeWindow]:
    in_force = [
        line for line in templates if line.weekday == day.weekday() and line.is_valid_on(day)
    ]
    if not in_force:
        return []
    latest = max(line.valid_from for line in in_force)
    return [line.window for line in in_force if line.valid_from == latest]


def _unfold(day: date, window: TimeWindow, zone: ZoneInfo) -> TimeRange:
    return TimeRange(local_to_utc(day, window.start, zone), local_to_utc(day, window.end, zone))
