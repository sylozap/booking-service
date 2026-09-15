"""Bodies of ``/api/v1/users/{id}/roles``."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from barber_auth.domain.roles import Role

__all__ = ["RoleGrantRequest", "RoleGrantResponse", "RoleRevokeRequest"]


class RoleGrantRequest(BaseModel):
    """The role to grant, and the salon it applies to.

    ``salon_id`` is required for ``salon_admin`` and optional for ``master``,
    where omitting it makes a global grant. ``client`` is listed so OpenAPI
    shows every role, but it cannot be granted.
    """

    model_config = ConfigDict(extra="forbid")

    role: Role
    salon_id: UUID | None = None


class RoleRevokeRequest(BaseModel):
    """Which grant to take away, identified by role and salon."""

    model_config = ConfigDict(extra="forbid")

    role: Role
    salon_id: UUID | None = None


class RoleGrantResponse(BaseModel):
    """The grant that now exists."""

    user_id: UUID
    role: str
    salon_id: UUID | None
