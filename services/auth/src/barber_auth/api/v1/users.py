"""Managing the roles of a user.

Both endpoints are limited to the roles that may grant roles; the salon scope
is checked by the scenario. A change reaches the user's access token on their
next refresh.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from barber_auth.api.v1.dependencies import GrantRoleScenario, RevokeRoleScenario
from barber_auth.domain.identifiers import SalonId, UserId
from barber_auth.schemas.users import RoleGrantRequest, RoleGrantResponse, RoleRevokeRequest
from barber_common.auth import Principal, require_roles

__all__ = ["router"]

router = APIRouter(prefix="/users", tags=["users"])

# Who may reach these endpoints at all. Which of them may grant which role, and
# where, is decided in the scenario: a dependency cannot see the salon in the
# body, and a rule split across two places is a rule that will disagree.
RoleAdministrator = Annotated[Principal, Depends(require_roles("super_admin", "salon_admin"))]


@router.post(
    "/{user_id}/roles",
    status_code=status.HTTP_201_CREATED,
    summary="Grant a role to a user",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "The caller may not grant this role here"},
        status.HTTP_404_NOT_FOUND: {"description": "No such user"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Role cannot be granted this way"},
    },
)
async def grant_role(
    user_id: UUID,
    body: RoleGrantRequest,
    caller: RoleAdministrator,
    scenario: GrantRoleScenario,
) -> RoleGrantResponse:
    """Give a user a role.

    A `super_admin` grants any role. A `salon_admin` grants `master`, and only
    in a salon they administer. `client` cannot be granted and answers `422`.

    The new role appears in the user's access token after their next
    `refresh`, within fifteen minutes. Granting a role the user already holds
    answers `201` again.
    """
    granted = await scenario.execute(
        caller=caller,
        user_id=UserId(user_id),
        role=body.role,
        salon_id=SalonId(body.salon_id) if body.salon_id is not None else None,
    )
    return RoleGrantResponse(
        user_id=granted.user_id,
        role=granted.role.value,
        salon_id=granted.salon_id,
    )


@router.delete(
    "/{user_id}/roles",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Take a role away from a user",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "The caller may not revoke this role here"},
        status.HTTP_404_NOT_FOUND: {"description": "No such user, or the role is not held"},
    },
)
async def revoke_role(
    user_id: UUID,
    body: RoleRevokeRequest,
    caller: RoleAdministrator,
    scenario: RevokeRoleScenario,
) -> None:
    """Remove a grant.

    Allowed to whoever may grant the role. The role disappears from the user's
    next access token, within fifteen minutes; `POST /api/v1/auth/logout` ends
    a session immediately.
    """
    await scenario.execute(
        caller=caller,
        user_id=UserId(user_id),
        role=body.role,
        salon_id=SalonId(body.salon_id) if body.salon_id is not None else None,
    )
