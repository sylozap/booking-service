"""Bodies of ``/api/v1/salons``."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

# The policy defaults are taken from the model, where the database defaults are
# defined.
from barber_catalog.models.salon import (
    DEFAULT_BOOKING_HORIZON_DAYS,
    DEFAULT_BOOKING_MIN_LEAD_MIN,
    DEFAULT_CANCEL_DEADLINE_MIN,
    DEFAULT_SLOT_STEP_MIN,
)

__all__ = ["SalonCreateRequest", "SalonResponse", "SalonUpdateRequest", "TimezoneName"]


def _known_timezone(value: str) -> str:
    """Refuse anything that is not an IANA identifier.

    Raises ``ValueError``, which pydantic reports as ``422``.
    """
    try:
        ZoneInfo(value)
    except (ValueError, KeyError) as error:
        raise ValueError(f"{value!r} is not a known IANA time zone identifier") from error
    return value


TimezoneName = Annotated[
    str,
    AfterValidator(_known_timezone),
    Field(
        max_length=64,
        description="IANA time zone of the salon, for example `Europe/Moscow`.",
        examples=["Europe/Moscow"],
    ),
]


class SalonPolicyFields(BaseModel):
    """The four booking policies of a salon, applied by ``booking``."""

    slot_step_min: int = Field(
        default=DEFAULT_SLOT_STEP_MIN,
        gt=0,
        le=240,
        description="Grid step a booking may start on, in minutes.",
    )
    booking_min_lead_min: int = Field(
        default=DEFAULT_BOOKING_MIN_LEAD_MIN,
        ge=0,
        description="How far ahead of the start a booking has to be made.",
    )
    booking_horizon_days: int = Field(
        default=DEFAULT_BOOKING_HORIZON_DAYS,
        gt=0,
        le=365,
        description="How far into the future bookings are accepted.",
    )
    cancel_deadline_min: int = Field(
        default=DEFAULT_CANCEL_DEADLINE_MIN,
        ge=0,
        description="Until when a client may cancel or reschedule without an administrator.",
    )


class SalonCreateRequest(SalonPolicyFields):
    """A new salon. The policies default to the documented values."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    address: str = Field(min_length=1, max_length=500)
    city: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=1, max_length=16)
    timezone: TimezoneName


class SalonUpdateRequest(BaseModel):
    """A change to a salon. Every field is optional; absent means untouched.

    ``null`` clears a field, while a missing field is left alone.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    address: str | None = Field(default=None, min_length=1, max_length=500)
    city: str | None = Field(default=None, min_length=1, max_length=120)
    phone: str | None = Field(default=None, min_length=1, max_length=16)
    timezone: TimezoneName | None = None

    slot_step_min: int | None = Field(default=None, gt=0, le=240)
    booking_min_lead_min: int | None = Field(default=None, ge=0)
    booking_horizon_days: int | None = Field(default=None, gt=0, le=365)
    cancel_deadline_min: int | None = Field(default=None, ge=0)

    is_active: bool | None = None


class SalonResponse(BaseModel):
    """A salon as the API shows it."""

    id: UUID
    name: str
    description: str | None
    address: str
    city: str
    phone: str
    timezone: str

    slot_step_min: int
    booking_min_lead_min: int
    booking_horizon_days: int
    cancel_deadline_min: int

    is_active: bool
    created_at: datetime
    updated_at: datetime
