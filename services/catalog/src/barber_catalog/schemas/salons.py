"""Bodies of ``/api/v1/salons``."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

# The four policy defaults come from the model, where the database default
# of the same value lives. One source rather than two that have to agree:
# only the numbers are imported, never the ORM class -- what section 5 of
# docs/CODING_STANDARDS.md forbids is a model reaching an API response, and
# these are the values OpenAPI has to publish.
from barber_catalog.models.salon import (
    DEFAULT_BOOKING_HORIZON_DAYS,
    DEFAULT_BOOKING_MIN_LEAD_MIN,
    DEFAULT_CANCEL_DEADLINE_MIN,
    DEFAULT_SLOT_STEP_MIN,
)

__all__ = ["SalonCreateRequest", "SalonResponse", "SalonUpdateRequest", "TimezoneName"]


def _known_timezone(value: str) -> str:
    """Refuse anything that is not an IANA identifier.

    A ``ValueError``, so pydantic turns it into the platform's ordinary
    ``422`` with a ``violations`` entry naming the field. The same rule is
    restated in :class:`~barber_catalog.models.salon.Salon` as the backstop for
    write paths that do not come through a request body; models may not import
    this module, and both checks are one call to ``ZoneInfo``.
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
    """The four settings of docs/02-domain-rules.md.

    Stored on the salon and applied in ``booking``: the salon owns the rule,
    the service holding the bookings enforces it, and the internal endpoint of
    T2.6 is how the second learns the first.
    """

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

    Absent and null are different: ``description: null`` clears the text,
    ``description`` missing leaves it alone. That is what ``exclude_unset``
    gives the scenario, and it is why this cannot simply reuse the create
    schema with defaults.
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
