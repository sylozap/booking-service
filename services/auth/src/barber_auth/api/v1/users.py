"""Managing the roles of a user.

Both endpoints are closed to everyone but the two roles that may hand roles
out, and the finer rule -- which salon -- is checked by the scenario, because
only it knows which salon the request is about.

**A granted role does not appear in a token that already exists.** Access
tokens are verified without asking anyone (ADR-0010), so what a token says
about its holder stays true until it expires; the new role reaches the user on
their next refresh, within fifteen minutes. It is stated in the description of
the endpoint below because a client integrating against this API will otherwise
grant a role, retry immediately, and conclude the grant failed.
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

    A `super_admin` grants anything. A `salon_admin` grants `master`, and only
    inside a salon they administer -- **not** another `salon_admin`, not even
    in their own salon: handing over a salon is a decision for whoever granted
    it in the first place.

    `client` cannot be granted here at all and answers `422`: it arrives with
    registration, and a second source for it is a second way for the two to
    disagree. That is not a permissions problem, which is why it is not a
    `403` even for a caller who has no permissions either.

    **The new role does not appear in access tokens the user already holds.**
    It reaches them on their next `refresh`, and so within fifteen minutes.
    That is a consequence of verifying tokens without asking anyone
    ([ADR-0010](docs/adr/0010-jwt-verified-in-services.md)), not an oversight.

    Granting a role the user already holds answers `201` again rather than a
    conflict: the requested state is the state that exists, and a retried
    request should not look like a failure.
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

    Whoever may hand a role out may take it back, and nobody else: without that
    symmetry a `salon_admin` could appoint a master they then could not remove.

    **Revoking does not invalidate the tokens the user is holding.** The role
    disappears from their next access token, within fifteen minutes. Ending a
    session immediately is what `POST /api/v1/auth/logout` is for.
    """
    await scenario.execute(
        caller=caller,
        user_id=UserId(user_id),
        role=body.role,
        salon_id=SalonId(body.salon_id) if body.salon_id is not None else None,
    )
