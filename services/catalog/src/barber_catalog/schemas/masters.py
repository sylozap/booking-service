"""Bodies of ``/api/v1/masters`` and ``/api/v1/salons/{id}/masters``."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "MasterCardResponse",
    "MasterCreateRequest",
    "MasterResponse",
    "MasterUpdateRequest",
    "OfferedServiceResponse",
]


class MasterCreateRequest(BaseModel):
    """A new master profile inside a salon."""

    model_config = ConfigDict(extra="forbid")

    salon_id: UUID = Field(description="The salon this master works in.")
    user_id: UUID = Field(
        description=(
            "The account in `auth` behind this profile. Not verified here: "
            "a cross-service check of a profile field does not pay for itself."
        )
    )
    display_name: str = Field(min_length=1, max_length=200)
    bio: str | None = Field(default=None, max_length=5000)
    photo_url: str | None = Field(default=None, max_length=1000)
    specialization: str | None = Field(default=None, max_length=200)


class MasterUpdateRequest(BaseModel):
    """A change to a profile. Absent fields are left alone.

    ``salon_id`` and ``user_id`` cannot be changed. ``is_active`` is changed only
    through the deactivate and activate endpoints.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    bio: str | None = Field(default=None, max_length=5000)
    photo_url: str | None = Field(default=None, max_length=1000)
    specialization: str | None = Field(default=None, max_length=200)


class MasterResponse(BaseModel):
    """A master profile as the API shows it."""

    id: UUID
    salon_id: UUID
    user_id: UUID
    display_name: str
    bio: str | None
    photo_url: str | None
    specialization: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class OfferedServiceResponse(BaseModel):
    """One service on a master's card, at the price that master charges.

    ``price`` and ``duration_min`` are resolved; the salon's base figures are
    shown alongside.
    """

    service_id: UUID
    name: str
    description: str | None
    price: Decimal
    currency: str
    duration_min: int

    base_price: Decimal
    base_duration_min: int


class MasterCardResponse(MasterResponse):
    """A profile together with everything that master offers."""

    services: list[OfferedServiceResponse]
