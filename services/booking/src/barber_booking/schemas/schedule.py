"""Bodies of ``/api/v1/masters/{id}/schedule`` and ``/settings``.

All times are wall-clock times of the salon, without a zone: a schedule is
written the way the salon reads it, and unfolded into instants on the way to
availability.
"""

from __future__ import annotations

from datetime import date, time
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from barber_booking.domain.schedule import ExceptionKind

__all__ = [
    "MasterSettingsResponse",
    "MasterSettingsUpdateRequest",
    "ScheduleExceptionList",
    "ScheduleExceptionRequest",
    "ScheduleExceptionResponse",
    "ScheduleVersion",
    "WeeklyScheduleRequest",
    "WeeklyScheduleResponse",
    "WorkingInterval",
]

# More than enough for a week of split shifts, and a bound on what one request
# may ask the database to write.
MAX_INTERVALS = 70
MAX_BUFFER_MIN = 240


def _check_local(*moments: time | None) -> None:
    if any(moment is not None and moment.tzinfo is not None for moment in moments):
        raise ValueError("times are the salon's local wall-clock time, without an offset")


class WorkingInterval(BaseModel):
    """Hours on one weekday."""

    model_config = ConfigDict(extra="forbid")

    weekday: int = Field(ge=0, le=6, description="0 is Monday, 6 is Sunday.")
    start_time: time
    end_time: time

    @model_validator(mode="after")
    def _ends_after_it_starts(self) -> Self:
        _check_local(self.start_time, self.end_time)
        if self.end_time <= self.start_time:
            raise ValueError("end_time has to be after start_time")
        return self


class WeeklyScheduleRequest(BaseModel):
    """The whole weekly template, from a date on."""

    model_config = ConfigDict(extra="forbid")

    valid_from: date | None = Field(
        default=None,
        description="The first date of the new template. Today in the salon if absent.",
    )
    intervals: list[WorkingInterval] = Field(
        max_length=MAX_INTERVALS,
        description="Every working interval of the week. Empty means no work at all.",
    )


class ScheduleVersion(BaseModel):
    """One version of the template and the dates it covers."""

    valid_from: date
    valid_to: date | None = Field(description="Inclusive; null means until further notice.")
    intervals: list[WorkingInterval]


class WeeklyScheduleResponse(BaseModel):
    """The template in force today and every version planned after it."""

    master_id: UUID
    timezone: str = Field(description="The IANA zone every time here is written in.")
    versions: list[ScheduleVersion]


class ScheduleExceptionRequest(BaseModel):
    """A change to one date."""

    model_config = ConfigDict(extra="forbid")

    effective_on: date
    kind: ExceptionKind
    start_time: time | None = None
    end_time: time | None = None
    reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _hours_match_the_kind(self) -> Self:
        _check_local(self.start_time, self.end_time)
        if self.kind is ExceptionKind.DAY_OFF:
            if self.start_time is not None or self.end_time is not None:
                raise ValueError("a day off has no hours")
            return self
        if self.start_time is None or self.end_time is None:
            raise ValueError(f"{self.kind} needs start_time and end_time")
        if self.end_time <= self.start_time:
            raise ValueError("end_time has to be after start_time")
        return self


class ScheduleExceptionResponse(BaseModel):
    id: UUID
    effective_on: date
    kind: ExceptionKind
    start_time: time | None
    end_time: time | None
    reason: str | None


class ScheduleExceptionList(BaseModel):
    items: list[ScheduleExceptionResponse]


class MasterSettingsUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    buffer_after_min: int = Field(
        ge=0,
        le=MAX_BUFFER_MIN,
        description="Minutes the master stays occupied after each booking.",
    )


class MasterSettingsResponse(BaseModel):
    master_id: UUID
    salon_id: UUID
    timezone: str
    buffer_after_min: int
    is_active: bool
