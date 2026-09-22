"""Cutting working time into the starts a client may book.

The reference implementation. The availability query does the same in SQL on
the hot path, and a reconciliation test holds the two to the same answers.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from barber_booking.domain.time_range import TimeRange

__all__ = ["slot_starts"]


def slot_starts(
    *,
    work: Sequence[TimeRange],
    busy: Sequence[TimeRange],
    duration_min: int,
    buffer_min: int,
    step_min: int,
) -> list[datetime]:
    """Every start on the grid that a booking may take.

    The grid begins at the start of each working interval. A start is good when
    the service fits inside the interval, and the service together with the
    buffer after it touches nobody else's time. The buffer does not have to fit
    in the interval: cleaning up after the last client may run past the shift.
    """
    if duration_min <= 0:
        raise ValueError("the duration of a service has to be positive")
    if step_min <= 0:
        raise ValueError("the step of the grid has to be positive")
    if buffer_min < 0:
        raise ValueError("the buffer cannot be negative")

    duration = timedelta(minutes=duration_min)
    occupied = timedelta(minutes=duration_min + buffer_min)
    step = timedelta(minutes=step_min)

    starts: set[datetime] = set()
    for interval in work:
        start = interval.start
        while start + duration <= interval.end:
            taken = TimeRange(start, start + occupied)
            if not any(taken.overlaps(other) for other in busy):
                starts.add(start)
            start += step
    return sorted(starts)
