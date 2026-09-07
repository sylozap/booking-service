"""Bodies of ``/api/v1/users/{id}/roles``."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from barber_auth.domain.roles import Role

__all__ = ["RoleGrantRequest", "RoleGrantResponse", "RoleRevokeRequest"]


class RoleGrantRequest(BaseModel):
    """The role to hand out, and the salon it applies to.

    ``salon_id`` is required for ``salon_admin`` and meaningful for ``master``;
    a ``master`` granted without one is a master everywhere, which only a
    ``super_admin`` may mean.

    The enum admits ``client`` so that OpenAPI documents the whole set of roles
    the platform has; the scenario is what answers that this one is not handed
    out here. Splitting the vocabulary in two would leave a reader of the
    contract guessing why one member is missing.
    """

    model_config = ConfigDict(extra="forbid")

    role: Role
    salon_id: UUID | None = None


class RoleRevokeRequest(BaseModel):
    """Which grant to take away.

    The role and the salon rather than an identifier of the grant: a global
    role and a role scoped to a salon are different grants of the same name,
    and the surrogate key that distinguishes them in the table is not something
    the API hands out (docs/CODING_STANDARDS.md section 9).
    """

    model_config = ConfigDict(extra="forbid")

    role: Role
    salon_id: UUID | None = None


class RoleGrantResponse(BaseModel):
    """The grant that now exists."""

    user_id: UUID
    role: str
    salon_id: UUID | None
