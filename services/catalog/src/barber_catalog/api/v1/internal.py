"""The internal API of the catalog: one endpoint, one caller.

Not part of the public contract and not published through the gateway. The
prefix is ``/internal/v1`` and the router is mounted separately from
``/api/v1`` for exactly that reason.

**A user token does not open this, however privileged.** The dependency demands
``typ=service``, so a token minted for a customer is refused before the scope
is even looked at -- including a `super_admin`'s. That is the property the
whole machine-to-machine flow exists for (docs/04-api-contracts.md).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from barber_catalog.api.v1.dependencies import ReadMasterServiceDetailsScenario
from barber_catalog.domain.identifiers import MasterId, ServiceId
from barber_common.auth import Principal, require_service_token
from barber_common.contracts.catalog import CATALOG_READ_SCOPE, MasterServiceDetails

__all__ = ["router"]

router = APIRouter(prefix="/internal/v1", tags=["internal"], include_in_schema=False)

InternalCaller = Annotated[Principal, Depends(require_service_token(CATALOG_READ_SCOPE))]


@router.get(
    "/masters/{master_id}/services/{service_id}",
    summary="Everything booking needs about one master offering one service",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "A service token is required"},
        status.HTTP_403_FORBIDDEN: {"description": "The token does not carry catalog:read"},
        status.HTTP_404_NOT_FOUND: {
            "description": "No such master, or this master does not offer this service"
        },
    },
)
async def read_master_service_details(
    master_id: UUID,
    service_id: UUID,
    caller: InternalCaller,
    scenario: ReadMasterServiceDetailsScenario,
) -> MasterServiceDetails:
    """The one call `booking` makes before it lays out a day.

    Returns whether the master is active, the price and duration **this**
    master charges, a snapshot of the service name, and the salon's time zone
    together with its four booking policies -- everything availability is
    computed from and everything a booking is created from, in one consistent
    read.

    A deactivated master is reported through `master_active`, not refused: that
    is what lets `booking` answer `master_inactive` instead of treating a
    policy decision as a failed upstream call. A master who does not offer this
    service is `404`, which `booking` turns into `service_not_offered`.

    The response schema is `barber_common.contracts.catalog.MasterServiceDetails`,
    imported by both sides, so a change to it breaks the type check rather than
    the runtime.
    """
    return await scenario.execute(
        master_id=MasterId(master_id),
        service_id=ServiceId(service_id),
    )
