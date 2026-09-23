"""Bodies of ``GET /api/v1/availability``."""

from __future__ import annotations

import datetime
from uuid import UUID

from pydantic import BaseModel, Field

__all__ = ["AvailabilityResponse", "DayAvailability"]


class DayAvailability(BaseModel):
    """The free starts of one date of the salon's calendar."""

    # Annotated through the module: a field called ``date`` whose type is also
    # called ``date`` is a name pydantic cannot resolve.
    date: datetime.date = Field(description="The date in the salon's time zone.")
    slots: list[datetime.datetime] = Field(description="Starts in UTC, in order.")


class AvailabilityResponse(BaseModel):
    """What a client needs to draw a calendar of one master and one service."""

    master_id: UUID
    service_id: UUID
    timezone: str = Field(description="The IANA zone the dates below are counted in.")
    master_active: bool = Field(
        description="False means the master takes no bookings; the days are then empty."
    )
    duration_min: int = Field(description="How long this master takes for this service.")
    days: list[DayAvailability]
