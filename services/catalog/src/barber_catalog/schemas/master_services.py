"""Bodies of ``/api/v1/masters/{id}/services/{sid}``."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["MasterServiceRequest", "MasterServiceResponse"]


class MasterServiceRequest(BaseModel):
    """What a master charges for a service, and how long they take.

    Both fields are optional and independent. Omitting one -- or sending it as
    ``null`` -- means "the salon's figure", which is how an override is
    removed: there is no separate endpoint for clearing one, because ``PUT``
    already means "this is the whole state of the link".

    A body of ``{}`` is therefore valid and means "this master offers this
    service at the salon's price, for the salon's duration".
    """

    model_config = ConfigDict(extra="forbid")

    price_override: Decimal | None = Field(
        default=None,
        ge=0,
        max_digits=10,
        decimal_places=2,
        description="What this master charges. `null` means the salon's base price.",
    )
    duration_override: int | None = Field(
        default=None,
        gt=0,
        le=24 * 60,
        description="How long this master takes. `null` means the salon's base duration.",
    )


class MasterServiceResponse(BaseModel):
    """The link as it now stands, with the figures already resolved.

    ``price`` and ``duration_min`` are what a client would be charged and how
    long they would be booked for -- the override where there is one, the
    salon's base figure otherwise. Both sides are returned so the caller can
    see what their request actually did.
    """

    master_id: UUID
    service_id: UUID

    price: Decimal
    duration_min: int
    currency: str

    price_override: Decimal | None
    duration_override: int | None
    base_price: Decimal
    base_duration_min: int

    created_at: datetime
    updated_at: datetime
