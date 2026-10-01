"""Bodies of ``/api/v1/masters`` and ``/api/v1/salons/{id}/masters``."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# The answers are shared contracts: the gateway reads the card of a master, see
# barber_common.contracts.catalog.
__all__ = ["MasterCreateRequest", "MasterUpdateRequest"]


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
