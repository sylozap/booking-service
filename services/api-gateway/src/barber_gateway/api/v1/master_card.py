"""``GET /api/v1/masters/{id}/card``: the one endpoint the gateway answers itself."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from starlette.requests import Request

from barber_gateway.api.dependencies import admit
from barber_gateway.schemas.master_card import MasterCard
from barber_gateway.services.master_card import ReadMasterCard
from barber_gateway.settings import GatewaySettings

__all__ = ["router"]

router = APIRouter(prefix="/masters", tags=["master card"])


def get_read_master_card(request: Request) -> ReadMasterCard:
    """The scenario, over the clients the lifespan built."""
    settings: GatewaySettings = request.app.state.settings
    return ReadMasterCard(
        catalog=request.app.state.catalog,
        booking=request.app.state.booking,
        window_days=settings.card_slots_window_days,
        slots_limit=settings.card_slots_limit,
    )


ReadMasterCardScenario = Annotated[ReadMasterCard, Depends(get_read_master_card)]


@router.get(
    "/{master_id}/card",
    summary="Profile, services and the nearest free slots of a master",
    # The token, if one is sent, and the limits, like any proxied request.
    dependencies=[Depends(admit)],
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "No such master"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "The master does not offer this service, or an id is malformed"
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "The catalog is not answering"},
    },
)
async def read_master_card(
    master_id: UUID,
    scenario: ReadMasterCardScenario,
    service_id: Annotated[
        UUID | None, Query(description="The service to show free slots for.")
    ] = None,
) -> MasterCard:
    """Everything the screen of a master needs, in one request.

    **Open to anonymous callers**, like the catalog and the availability it is
    made of. The profile and the services come from catalog, the nearest free
    starts for `service_id` from booking; both are asked at once.

    Without `service_id` there are no slots and `slots_status` is
    `not_requested`. If booking does not answer, the card is still served:
    the slots are empty and `slots_status` is `unavailable`, which is not the
    same as a master with nothing free (`ok` and an empty list). If catalog
    does not answer, there is no card: `503`.

    Like availability itself, the slots are an estimate, not a reservation.
    """
    return await scenario.execute(master_id=master_id, service_id=service_id)
