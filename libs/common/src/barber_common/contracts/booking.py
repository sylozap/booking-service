"""Contracts of the booking endpoints other services call.

``GET /api/v1/availability`` is public, and the gateway calls it for the
nearest free slots on the card of a master. :class:`AvailabilityResponse` is
what booking answers and what the gateway reads.
"""

from __future__ import annotations

import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["AvailabilityResponse", "DayAvailability"]


class DayAvailability(BaseModel):
    """The free starts of one date of the salon's calendar."""

    model_config = ConfigDict(extra="ignore")

    # Annotated through the module: a field called ``date`` whose type is also
    # called ``date`` is a name pydantic cannot resolve.
    date: datetime.date = Field(description="The date in the salon's time zone.")
    slots: list[datetime.datetime] = Field(description="Starts in UTC, in order.")


class AvailabilityResponse(BaseModel):
    """What a client needs to draw a calendar of one master and one service."""

    model_config = ConfigDict(extra="ignore")

    master_id: UUID
    service_id: UUID
    timezone: str = Field(description="The IANA zone the dates below are counted in.")
    master_active: bool = Field(
        description="False means the master takes no bookings; the days are then empty."
    )
    duration_min: int = Field(description="How long this master takes for this service.")
    days: list[DayAvailability]
