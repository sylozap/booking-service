"""The internal API of the catalog.

Mounted at ``/internal/v1`` and not published through the gateway. Requires a
service token; a user token is refused whatever its roles.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from barber_catalog.api.v1.dependencies import (
    ReadMasterProfileScenario,
    ReadMasterServiceDetailsScenario,
)
from barber_catalog.domain.identifiers import MasterId, ServiceId
from barber_common.auth import Principal, require_service_token
from barber_common.contracts.catalog import (
    CATALOG_READ_SCOPE,
    MasterProfile,
    MasterServiceDetails,
)

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
    """Everything `booking` needs about one master offering one service.

    Returns whether the master is active, the price and duration this master
    charges, a snapshot of the service name, and the salon's time zone with its
    four booking policies.

    A deactivated master is reported through `master_active`. A master who does
    not offer this service answers `404`.
    """
    return await scenario.execute(
        master_id=MasterId(master_id),
        service_id=ServiceId(service_id),
    )


@router.get(
    "/masters/{master_id}",
    summary="Who a master is: account, salon and time zone",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "A service token is required"},
        status.HTTP_403_FORBIDDEN: {"description": "The token does not carry catalog:read"},
        status.HTTP_404_NOT_FOUND: {"description": "No such master"},
    },
)
async def read_master_profile(
    master_id: UUID,
    caller: InternalCaller,
    scenario: ReadMasterProfileScenario,
) -> MasterProfile:
    """The facts `master.created` carries, as they stand now.

    For `booking`, which reads it once for a master whose `master.created` it
    never received. A deactivated master is described too, with `is_active`
    false.
    """
    return await scenario.execute(master_id=MasterId(master_id))
