"""Master profiles: who works in a salon, and what they offer.

Reading is open to anonymous callers and writing is not, for the reason given
in :mod:`barber_catalog.api.v1.salons`.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from barber_catalog.api.v1.dependencies import (
    CreateMasterScenario,
    ListSalonMastersScenario,
    ReadMasterCardScenario,
    UpdateMasterScenario,
)
from barber_catalog.domain.identifiers import MasterId, SalonId
from barber_catalog.schemas.masters import (
    MasterCardResponse,
    MasterCreateRequest,
    MasterResponse,
    MasterUpdateRequest,
)
from barber_catalog.services.authorization import SALON_ADMINISTRATORS
from barber_common.auth import Principal, require_roles
from barber_common.pagination import Page, Pagination

__all__ = ["router", "salon_masters_router"]

router = APIRouter(prefix="/masters", tags=["masters"])
# The staff of one salon hangs off the salon, not off the master collection:
# the path says whose masters these are, and the listing needs it anyway.
salon_masters_router = APIRouter(prefix="/salons", tags=["masters"])

SalonAdministrator = Annotated[Principal, Depends(require_roles(*SALON_ADMINISTRATORS))]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Add a master to a salon",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Not an administrator of this salon"},
        status.HTTP_404_NOT_FOUND: {"description": "No such salon"},
    },
)
async def create_master(
    body: MasterCreateRequest,
    caller: SalonAdministrator,
    scenario: CreateMasterScenario,
) -> MasterResponse:
    """Create a master profile.

    A `super_admin` may add a master to any salon; a `salon_admin` only to the
    salon their grant names.

    `user_id` names the account in `auth` this master signs in with. **It is
    not verified**: checking it would make every profile write depend on the
    availability of `auth`, and an account can be closed a second after the
    check anyway. A profile naming an account that does not exist is a data
    error to be corrected, not a request to be refused.

    Creating a profile publishes `master.created`, which is what gives the
    master a settings row and, with it, the ability to have a schedule in
    `booking`. The event is written in the same transaction as the profile, so
    a master that exists always has one on the way.
    """
    return await scenario.execute(caller=caller, body=body)


@router.get(
    "/{master_id}",
    summary="Read a master card",
    responses={status.HTTP_404_NOT_FOUND: {"description": "No such master"}},
)
async def read_master_card(
    master_id: UUID,
    scenario: ReadMasterCardScenario,
) -> MasterCardResponse:
    """A master profile together with every service they offer.

    **Open to anonymous callers.** Each service carries the price and duration
    *this master* charges -- their own override where they set one, the salon's
    base figure otherwise -- with the base figures alongside, so a client can
    see where a master differs from the salon list.

    A deactivated master is still returned, with `is_active` false. Their
    bookings still name them, and hiding the profile would leave a client
    looking at a booking they cannot attribute to anyone.
    """
    return await scenario.execute(master_id=MasterId(master_id))


@router.patch(
    "/{master_id}",
    summary="Change a master profile",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Not an administrator of this salon"},
        status.HTTP_404_NOT_FOUND: {"description": "No such master"},
    },
)
async def update_master(
    master_id: UUID,
    body: MasterUpdateRequest,
    caller: SalonAdministrator,
    scenario: UpdateMasterScenario,
) -> MasterResponse:
    """Change a profile. Only the fields present in the body are touched.

    The salon and the account cannot be changed here: both are part of what
    makes this profile *this* profile, and moving a master between salons is
    creating a second profile, not editing the first.
    """
    return await scenario.execute(caller=caller, master_id=MasterId(master_id), body=body)


@salon_masters_router.get(
    "/{salon_id}/masters",
    summary="List the masters of a salon",
    responses={status.HTTP_404_NOT_FOUND: {"description": "No such salon"}},
)
async def list_salon_masters(
    salon_id: UUID,
    pagination: Pagination,
    scenario: ListSalonMastersScenario,
    is_active: Annotated[
        bool | None,
        Query(description="Show only active masters, or only deactivated ones."),
    ] = None,
) -> Page[MasterResponse]:
    """The staff of a salon, alphabetically, one page at a time.

    **Open to anonymous callers.** Both active and deactivated masters are
    listed unless `is_active` says otherwise: a visitor picking a master wants
    `is_active=true`, an administrator reviewing their staff wants everyone.

    Paging is by cursor; see `GET /api/v1/salons` for what that means.
    """
    return await scenario.execute(
        salon_id=SalonId(salon_id), request=pagination, is_active=is_active
    )
