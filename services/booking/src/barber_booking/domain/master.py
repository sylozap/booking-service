"""A master as booking sees them: the owner of a schedule."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from barber_booking.domain.identifiers import MasterId, SalonId, UserId

__all__ = ["DEFAULT_BUFFER_AFTER_MIN", "MasterSettings", "validate_timezone"]

# A master starts with no pause between clients until they ask for one.
DEFAULT_BUFFER_AFTER_MIN = 0


def validate_timezone(name: str) -> None:
    """Refuse a zone that cannot be resolved: a schedule in it could not be unfolded."""
    try:
        ZoneInfo(name)
    except (ValueError, KeyError) as error:
        raise ValueError(f"{name!r} is not a known IANA time zone") from error


@dataclass(frozen=True, slots=True)
class MasterSettings:
    """What booking keeps about one master."""

    master_id: MasterId
    salon_id: SalonId
    user_id: UserId
    timezone: str
    buffer_after_min: int
    is_active: bool

    def __post_init__(self) -> None:
        validate_timezone(self.timezone)
        if self.buffer_after_min < 0:
            raise ValueError("the buffer cannot be negative")

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def today(self, now: datetime) -> date:
        """The salon's current date. The past ends at local midnight."""
        return now.astimezone(self.zone).date()
