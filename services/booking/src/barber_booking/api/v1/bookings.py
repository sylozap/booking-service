"""Creating a booking.

The only endpoint of the platform that demands ``Idempotency-Key``: a client
that retries after a lost connection must not end up with two appointments.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from barber_booking.api.v1.dependencies import CreateBookingScenario
from barber_booking.schemas.bookings import BookingCreateRequest, BookingResponse
from barber_common.auth import Principal, require_roles
from barber_common.idempotency import RequiredIdempotencyKey

__all__ = ["router"]

router = APIRouter(prefix="/bookings", tags=["bookings"])

# Everyone who registers holds this role, so the check does not narrow who may
# book; it is what keeps the endpoint closed to a service token.
Client = Annotated[Principal, Depends(require_roles("client"))]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Book a master for a service",
    responses={
        status.HTTP_409_CONFLICT: {
            "description": "The slot was taken while this request was in flight"
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": (
                "No Idempotency-Key, the key was used for another body, or the booking "
                "breaks one of the salon's windows"
            )
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "The catalog is not answering"},
    },
)
async def create_booking(
    body: BookingCreateRequest,
    caller: Client,
    request: RequiredIdempotencyKey,
    scenario: CreateBookingScenario,
) -> BookingResponse:
    """Create a booking for the caller and confirm it at once.

    The booking is written with a snapshot of the service -- its name, price
    and duration as they are now -- so a later change to the price list does
    not rewrite history.

    `Idempotency-Key` is mandatory. The same key with the same body answers the
    first answer again without booking anything twice; the same key with a
    different body is `422 idempotency_key_reuse`.

    The salon's windows are checked first: too close to the start is
    `booking_too_late`, too far ahead `booking_too_far`, a time the master does
    not work `slot_outside_schedule`, a deactivated master `master_inactive`
    and a service they do not offer `service_not_offered`.

    Whether the time is still free is decided by the database at the moment of
    the insert, not by the availability the client was shown. Losing that race
    is `409 slot_taken`.
    """
    return await scenario.execute(caller=caller, request=request, body=body)
