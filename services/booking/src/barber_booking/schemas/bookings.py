"""Bodies of ``/api/v1/bookings``."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

__all__ = [
    "BookingCancelRequest",
    "BookingCreateRequest",
    "BookingRescheduleRequest",
    "BookingResponse",
]

MAX_REASON_LENGTH = 500


class BookingCreateRequest(BaseModel):
    """An appointment a client asks for.

    The client is the caller: there is no field for it, and a body naming
    someone else would be an impersonation this endpoint has no reason to
    accept.
    """

    model_config = ConfigDict(extra="forbid")

    master_id: UUID
    service_id: UUID = Field(description="Decides the price and the duration of this booking.")
    start_at: AwareDatetime = Field(
        description="RFC 3339 with an offset. Has to be one of the starts availability offers."
    )


class BookingResponse(BaseModel):
    """A booking as the API shows it, snapshot included."""

    id: UUID
    salon_id: UUID
    master_id: UUID
    client_user_id: UUID

    service_id: UUID
    service_name: str = Field(description="As the service was called when this was booked.")
    price: Decimal
    currency: str
    duration_min: int
    buffer_min: int = Field(description="Minutes the master stays occupied after the service.")

    start_at: datetime
    end_at: datetime
    status: str
    reminder_at: datetime | None

    cancelled_at: datetime | None = None
    cancel_reason: str | None = None

    created_at: datetime


class BookingCancelRequest(BaseModel):
    """Why a visit is called off. The body may be left out entirely."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(
        default=None,
        max_length=MAX_REASON_LENGTH,
        description="Free text passed on to the other side.",
    )


class BookingRescheduleRequest(BaseModel):
    """The new start of a booking. Nothing else about it changes."""

    model_config = ConfigDict(extra="forbid")

    start_at: AwareDatetime = Field(
        description="RFC 3339 with an offset. Has to be one of the starts availability offers."
    )
