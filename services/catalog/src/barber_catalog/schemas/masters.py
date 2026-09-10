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

    ``salon_id`` and ``user_id`` are absent on purpose: moving a master to
    another salon or onto another account is not an edit of a profile, it is a
    different profile. Both are part of the identity the unique index is built
    on.

    ``is_active`` is absent for a stronger reason. Deactivating a master
    cancels every future booking they have, through an event ``booking``
    consumes (T2.8). A consequence like that cannot hang off a field of a
    general-purpose edit, where a client sending the whole profile back would
    trigger it by accident. It is ``POST .../deactivate`` and
    ``POST .../activate``, exactly as withdrawing a service is its own endpoint
    rather than a flag on this one.
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

    ``price`` and ``duration_min`` are already resolved -- the master's
    override where there is one, the salon's base figure otherwise. The base
    figures are shown next to them so a client can see that this master is
    dearer or slower than the salon's list, which is the question the card
    exists to answer.
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
    """A profile together with everything that master offers.

    One response rather than two requests: this is what a visitor opens when
    they pick a master, and the gateway aggregates it further with the free
    slots from `booking` (T6.4).
    """

    services: list[OfferedServiceResponse]
