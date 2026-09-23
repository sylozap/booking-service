"""A span of real time: ``[start, end)`` between two aware instants."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

__all__ = ["TimeRange"]


@dataclass(frozen=True, slots=True, order=True)
class TimeRange:
    """Half-open, like ``tstzrange(..., '[)')``: touching ranges do not overlap.

    Both ends carry a zone. A naive instant is refused rather than guessed.
    """

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("a time range needs instants with a time zone")
        if self.end <= self.start:
            raise ValueError("a time range has to end after it starts")

    def overlaps(self, other: TimeRange) -> bool:
        return self.start < other.end and other.start < self.end

    def minus(self, other: TimeRange) -> list[TimeRange]:
        """What is left of this range once ``other`` is cut out of it."""
        if not self.overlaps(other):
            return [self]

        rest = []
        if self.start < other.start:
            rest.append(TimeRange(self.start, other.start))
        if other.end < self.end:
            rest.append(TimeRange(other.end, self.end))
        return rest
