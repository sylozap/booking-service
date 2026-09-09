"""What a master offers, and on what terms.

Both endpoints are closed to everyone but the administrators of the salon the
master belongs to, and both are idempotent.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from barber_catalog.api.v1.dependencies import (
    LinkMasterServiceScenario,
    UnlinkMasterServiceScenario,
)
from barber_catalog.domain.identifiers import MasterId, ServiceId
from barber_catalog.schemas.master_services import MasterServiceRequest, MasterServiceResponse
from barber_catalog.services.authorization import SALON_ADMINISTRATORS
from barber_common.auth import Principal, require_roles

__all__ = ["router"]

router = APIRouter(prefix="/masters", tags=["master services"])

SalonAdministrator = Annotated[Principal, Depends(require_roles(*SALON_ADMINISTRATORS))]


@router.put(
    "/{master_id}/services/{service_id}",
    summary="Say that a master offers a service",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Not an administrator of this salon"},
        status.HTTP_404_NOT_FOUND: {"description": "No such master, or no such service"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "The service belongs to another salon, or has been archived"
        },
    },
)
async def link_master_service(
    master_id: UUID,
    service_id: UUID,
    body: MasterServiceRequest,
    caller: SalonAdministrator,
    scenario: LinkMasterServiceScenario,
) -> MasterServiceResponse:
    """State that this master offers this service, and on what terms.

    `PUT` states the **whole** terms of the link. An override left out of the
    body -- or sent as `null` -- is an override that no longer applies, and the
    salon's base figure takes over. A body of `{}` therefore means "at the
    salon's price, for the salon's duration", and is the way to clear an
    override that was set earlier.

    Repeating the call with the same body changes nothing and answers `200`
    again. An administrator setting the price list for a dozen masters retries,
    and a create that failed the second time would leave them checking each one
    by hand.

    The service has to belong to the master's own salon. If it does not, the
    answer is `422` and not `404`: both exist and the caller may see both, so
    what is wrong is the combination. An archived service cannot be taken up
    either -- archiving is how a salon stops offering something, and letting a
    master pick it back up would make the archive an opinion rather than a
    state.

    The response carries `price` and `duration_min` already resolved, next to
    the override and the base figure each came from.
    """
    return await scenario.execute(
        caller=caller,
        master_id=MasterId(master_id),
        service_id=ServiceId(service_id),
        body=body,
    )


@router.delete(
    "/{master_id}/services/{service_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Say that a master no longer offers a service",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Not an administrator of this salon"},
        status.HTTP_404_NOT_FOUND: {"description": "No such master"},
    },
)
async def unlink_master_service(
    master_id: UUID,
    service_id: UUID,
    caller: SalonAdministrator,
    scenario: UnlinkMasterServiceScenario,
) -> None:
    """Withdraw the service from this master's card.

    **Bookings that already exist are untouched.** A booking carries its own
    snapshot of the service name, price and duration, taken when it was made,
    so what a master offers today has no bearing on what was already agreed.

    The terms are kept rather than thrown away, so re-stating the link later
    does not mean entering the prices again.

    A link that is not there, or is already withdrawn, still answers `204`: the
    requested state is the state that holds. A master that does not exist is
    `404` -- that is a wrong request rather than a repeated one.
    """
    await scenario.execute(
        caller=caller,
        master_id=MasterId(master_id),
        service_id=ServiceId(service_id),
    )
