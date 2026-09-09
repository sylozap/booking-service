"""The price list of a salon: what is offered, at what price, for how long.

Reading is open to anonymous callers and writing is not, for the reason given
in :mod:`barber_catalog.api.v1.salons`.

**There is no `DELETE` here, and its absence is the guarantee.** Bookings refer
to a service by identifier, so a deleted row turns every past booking into a
dangling reference. Withdrawing an offering is `POST .../archive`. FastAPI
answers `405 Method Not Allowed` for the delete nobody wrote, which is a
stronger promise than a handler that raises -- there is no code path to get
wrong.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from barber_catalog.api.v1.dependencies import (
    ArchiveServiceScenario,
    CreateServiceScenario,
    ListSalonServicesScenario,
    ReadServiceScenario,
    UpdateServiceScenario,
)
from barber_catalog.domain.identifiers import SalonId, ServiceId
from barber_catalog.schemas.services import (
    ServiceCreateRequest,
    ServiceResponse,
    ServiceUpdateRequest,
)
from barber_catalog.services.authorization import SALON_ADMINISTRATORS
from barber_common.auth import Principal, require_roles
from barber_common.pagination import Page, Pagination

__all__ = ["router", "salon_services_router"]

router = APIRouter(prefix="/services", tags=["services"])
# The price list hangs off the salon that owns it; a single service is reached
# by its own identifier, because that is what a booking carries.
salon_services_router = APIRouter(prefix="/salons", tags=["services"])

SalonAdministrator = Annotated[Principal, Depends(require_roles(*SALON_ADMINISTRATORS))]


@salon_services_router.post(
    "/{salon_id}/services",
    status_code=status.HTTP_201_CREATED,
    summary="Add a service to a salon",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Not an administrator of this salon"},
        status.HTTP_404_NOT_FOUND: {"description": "No such salon"},
    },
)
async def create_service(
    salon_id: UUID,
    body: ServiceCreateRequest,
    caller: SalonAdministrator,
    scenario: CreateServiceScenario,
) -> ServiceResponse:
    """Add a service to the price list.

    `base_duration_min` and `base_price` are the salon's standard figures. A
    master may override either through
    `PUT /api/v1/masters/{id}/services/{sid}`; the figure that then counts is
    the override, and the base is what a master who sets none charges.

    Names are not unique. A salon may offer "Haircut" for adults and "Haircut"
    for children and tell them apart by description, and refusing that would be
    this service overruling a decision that belongs to the salon.
    """
    return await scenario.execute(caller=caller, salon_id=SalonId(salon_id), body=body)


@salon_services_router.get(
    "/{salon_id}/services",
    summary="List the services of a salon",
    responses={status.HTTP_404_NOT_FOUND: {"description": "No such salon"}},
)
async def list_salon_services(
    salon_id: UUID,
    pagination: Pagination,
    scenario: ListSalonServicesScenario,
) -> Page[ServiceResponse]:
    """The price list of a salon, alphabetically, one page at a time.

    **Open to anonymous callers.** Archived services never appear here and
    there is no parameter to ask for them: archiving is how a salon withdraws
    an offering, and a listing that could show withdrawn ones would need every
    caller to remember to filter. An archived service is fetched by identifier.

    Paging is by cursor; see `GET /api/v1/salons` for what that means.
    """
    return await scenario.execute(salon_id=SalonId(salon_id), request=pagination)


@router.get(
    "/{service_id}",
    summary="Read one service",
    responses={status.HTTP_404_NOT_FOUND: {"description": "No such service"}},
)
async def read_service(service_id: UUID, scenario: ReadServiceScenario) -> ServiceResponse:
    """One service, archived or not. **Open to anonymous callers.**

    Archived services are the point of this endpoint: a client looking at a
    booking from last year holds an identifier and needs a name for it.
    `is_archived` says which kind this is.
    """
    return await scenario.execute(service_id=ServiceId(service_id))


@router.patch(
    "/{service_id}",
    summary="Change a service",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Not an administrator of this salon"},
        status.HTTP_404_NOT_FOUND: {"description": "No such service"},
    },
)
async def update_service(
    service_id: UUID,
    body: ServiceUpdateRequest,
    caller: SalonAdministrator,
    scenario: UpdateServiceScenario,
) -> ServiceResponse:
    """Change a service. Only the fields present in the body are touched.

    **Changing the price does not change anything already agreed.** A master's
    override is a separate value and is untouched; a booking that already
    exists carries its own snapshot of the name and the price, taken when it
    was made. Only bookings made from now on see the new figure.

    Archiving is not one of the fields: it is `POST .../archive`.
    """
    return await scenario.execute(caller=caller, service_id=ServiceId(service_id), body=body)


@router.post(
    "/{service_id}/archive",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Withdraw a service",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Not an administrator of this salon"},
        status.HTTP_404_NOT_FOUND: {"description": "No such service"},
    },
)
async def archive_service(
    service_id: UUID,
    caller: SalonAdministrator,
    scenario: ArchiveServiceScenario,
) -> None:
    """Take a service off the price list without deleting it.

    The service leaves every listing and stays reachable by identifier, so the
    bookings that name it remain readable. **This is the only way to withdraw
    one** -- there is no `DELETE`, and there will not be one.

    Repeating the call changes nothing and still answers `204`: the requested
    state is the state that already holds, and a retry should not look like a
    failure.

    Links from masters to this service are left in place, so unarchiving does
    not require every master's price overrides to be entered again.
    """
    await scenario.execute(caller=caller, service_id=ServiceId(service_id))
