"""A master as booking sees them: the owner of a schedule."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from barber_booking.domain.identifiers import MasterId, SalonId, UserId

__all__ = ["MasterSettings"]


@dataclass(frozen=True, slots=True)
class MasterSettings:
    """What booking keeps about one master."""

    master_id: MasterId
    salon_id: SalonId
    user_id: UserId
    timezone: str
    buffer_after_min: int
    is_active: bool

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def today(self, now: datetime) -> date:
        """The salon's current date. The past ends at local midnight."""
        return now.astimezone(self.zone).date()
