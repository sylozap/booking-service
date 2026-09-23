"""The windows a booking has to fall inside.

The salon sets three of them -- how late a client may still book, how far
ahead, and on what grid -- and they are applied here, not where the row is
written. Whether the time is *free* is not decided here at all: that is the
exclusion constraint's answer, and the only one that counts.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from barber_booking.domain.errors import BookingTooFar, BookingTooLate, SlotOutsideSchedule
from barber_booking.domain.slots import slot_starts
from barber_booking.domain.time_range import TimeRange

__all__ = ["validate_horizon", "validate_lead_time", "validate_start_is_bookable"]


def validate_lead_time(start_at: datetime, *, now: datetime, lead_min: int) -> None:
    """Refuse a booking made too close to its own start.

    The salon needs the notice, and a booking in the past needs it most.
    """
    if start_at < now + timedelta(minutes=lead_min):
        raise BookingTooLate(f"This salon takes bookings at least {lead_min} minutes ahead")


def validate_horizon(
    start_at: datetime, *, now: datetime, horizon_days: int, zone: ZoneInfo
) -> None:
    """Refuse a booking further ahead than the salon plans.

    Counted in the salon's own calendar days, so the last allowed date is
    bookable until its local midnight rather than until this time of day.
    """
    last_date = now.astimezone(zone).date() + timedelta(days=horizon_days)
    if start_at >= _local_midnight_after(last_date, zone):
        raise BookingTooFar(f"This salon takes bookings up to {horizon_days} days ahead")


def validate_start_is_bookable(
    start_at: datetime,
    *,
    work: Sequence[TimeRange],
    duration_min: int,
    buffer_min: int,
    step_min: int,
) -> None:
    """Refuse a start that availability would never have offered.

    The same function that builds the offer decides this, with no bookings in
    the way: a start that is not on the grid, or whose service does not fit
    inside the working time, is refused whether or not the time is free.
    """
    offered = slot_starts(
        work=work,
        busy=(),
        duration_min=duration_min,
        buffer_min=buffer_min,
        step_min=step_min,
    )
    if start_at not in offered:
        raise SlotOutsideSchedule("The master does not work at this time")


def _local_midnight_after(day: date, zone: ZoneInfo) -> datetime:
    return datetime.combine(day + timedelta(days=1), time(), tzinfo=zone)
