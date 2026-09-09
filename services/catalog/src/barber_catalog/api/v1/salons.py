"""Salons: the shop window, and the policies every booking obeys.

**Reading is open to anonymous callers and writing is not.** Section 9 of
docs/CODING_STANDARDS.md requires a role check on every endpoint, and the two
`GET`s below deliberately have none: the catalog is what a visitor sees before
they have an account, and T6.2 lists it in the anonymous allowlist of the
gateway. The rule the standard is protecting -- that no endpoint is left open
by accident -- is met by saying so here and by the tests that assert both
halves: an anonymous read succeeds, and an anonymous write does not.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from barber_catalog.api.v1.dependencies import (
    CreateSalonScenario,
    ListSalonsScenario,
    ReadSalonScenario,
    UpdateSalonScenario,
)
from barber_catalog.domain.identifiers import SalonId
from barber_catalog.schemas.salons import SalonCreateRequest, SalonResponse, SalonUpdateRequest
from barber_catalog.services.authorization import SALON_ADMINISTRATORS
from barber_common.auth import Principal, require_roles
from barber_common.pagination import Page, Pagination

__all__ = ["router"]

router = APIRouter(prefix="/salons", tags=["salons"])

# Only a super_admin opens a salon: an administrator who could create salons
# could create the one they administer.
SuperAdministrator = Annotated[Principal, Depends(require_roles("super_admin"))]
# Who may reach a change at all. Whether it is *their* salon is decided in the
# scenario, which is the first place that knows which salon the request means.
SalonAdministrator = Annotated[Principal, Depends(require_roles(*SALON_ADMINISTRATORS))]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Open a salon",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Only a super administrator opens a salon"},
    },
)
async def create_salon(
    body: SalonCreateRequest,
    caller: SuperAdministrator,
    scenario: CreateSalonScenario,
) -> SalonResponse:
    """Create a salon, with the four booking policies it will be run by.

    The policies -- `slot_step_min`, `booking_min_lead_min`,
    `booking_horizon_days`, `cancel_deadline_min` -- are stored here and
    applied by `booking`. Omitting them takes the defaults of
    [docs/02-domain-rules.md](docs/02-domain-rules.md).

    `timezone` is an IANA identifier such as `Europe/Moscow`, never a UTC
    offset: an offset is correct for half the year, and a weekly schedule
    stored against one moves by an hour when the country changes its clocks.
    """
    return await scenario.execute(caller=caller, body=body)


@router.get(
    "",
    summary="List salons",
)
async def list_salons(
    pagination: Pagination,
    scenario: ListSalonsScenario,
    city: Annotated[str | None, Query(description="Show only salons in this city.")] = None,
) -> Page[SalonResponse]:
    """The salons on display, alphabetically, one page at a time.

    **Open to anonymous callers**: this is the shop window, and a visitor sees
    it before they have an account.

    Paging is by cursor and never by offset. `next_cursor` is opaque -- it
    encodes the sort key of the last item on the page, and the next request
    resumes after that row rather than counting rows before it. A salon added
    or removed between two page requests therefore cannot make an existing one
    be skipped or shown twice, which is exactly what an offset does. A `null`
    `next_cursor` means this was the last page.

    Closed salons are not listed. One remains readable by its own identifier.
    """
    return await scenario.execute(request=pagination, city=city)


@router.get(
    "/{salon_id}",
    summary="Read one salon",
    responses={status.HTTP_404_NOT_FOUND: {"description": "No such salon"}},
)
async def read_salon(salon_id: UUID, scenario: ReadSalonScenario) -> SalonResponse:
    """One salon with its policies. **Open to anonymous callers.**

    A closed salon is still returned here, unlike in the listing: a booking
    made while it was open still names it.
    """
    return await scenario.execute(salon_id=SalonId(salon_id))


@router.patch(
    "/{salon_id}",
    summary="Change a salon",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Not an administrator of this salon"},
        status.HTTP_404_NOT_FOUND: {"description": "No such salon"},
    },
)
async def update_salon(
    salon_id: UUID,
    body: SalonUpdateRequest,
    caller: SalonAdministrator,
    scenario: UpdateSalonScenario,
) -> SalonResponse:
    """Change a salon. Only the fields present in the body are touched.

    A `super_admin` may change any salon; a `salon_admin` only the salon their
    grant names. Both refusals answer `403` with the same body: separating them
    would let an administrator of one salon find out which other salons exist.

    A field sent as `null` is cleared; a field left out is left alone.
    """
    return await scenario.execute(caller=caller, salon_id=SalonId(salon_id), body=body)
