"""When a master is free for a service.

**Open to anonymous callers**, like reading the catalog: slots are part of the
shop window, and a visitor looks at them before creating an account. Writing a
booking is not open, and the gateway keeps this path in its rate-limited
whitelist.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from barber_booking.api.v1.dependencies import ReadAvailabilityScenario
from barber_booking.domain.identifiers import MasterId, ServiceId
from barber_common.contracts.booking import AvailabilityResponse

__all__ = ["router"]

router = APIRouter(prefix="/availability", tags=["availability"])


@router.get(
    "",
    summary="Free starts of one master for one service",
    responses={
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "The window is wider than two weeks, or service_id is missing"
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "The catalog is not answering"},
    },
)
async def read_availability(
    scenario: ReadAvailabilityScenario,
    master_id: Annotated[UUID, Query()],
    service_id: Annotated[UUID, Query(description="Mandatory: the duration depends on it.")],
    date_from: Annotated[date, Query(description="First date, in the salon's time zone.")],
    date_to: Annotated[
        date | None, Query(description="Last date, inclusive. Equal to date_from if absent.")
    ] = None,
) -> AvailabilityResponse:
    """Free starts, date by date, for this master and this service.

    `service_id` is mandatory: how long a master is busy depends on the pair,
    so a question without it has no answer. The window is at most two weeks.

    **The answer is an estimate, not a reservation.** A slot listed here can be
    taken by someone else before the booking is created; that request then
    answers `409 slot_taken` with alternatives. Nothing reserves time except a
    booking, and nothing decides whether time is free except the database.

    Dates are the salon's calendar dates and starts are instants in UTC. A
    deactivated master answers with `master_active` false and empty days rather
    than an error, and so does a master whose schedule has never been set.
    Dates beyond the salon's booking horizon come back empty.
    """
    return await scenario.execute(
        master_id=MasterId(master_id),
        service_id=ServiceId(service_id),
        date_from=date_from,
        date_to=date_to,
    )
