"""Bookings: creating one, and everything that happens to it afterwards.

Creation is the only endpoint of the platform that demands
``Idempotency-Key``: a client that retries after a lost connection must not
end up with two appointments. The operations on an existing booking are
idempotent by its state -- repeating one finds the work already done.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, status

from barber_booking.api.v1.dependencies import (
    CancelBookingScenario,
    CreateBookingScenario,
    RescheduleBookingScenario,
)
from barber_booking.domain.identifiers import BookingId
from barber_booking.schemas.bookings import (
    BookingCancelRequest,
    BookingCreateRequest,
    BookingRescheduleRequest,
    BookingResponse,
)
from barber_booking.services.authorization import BOOKING_ROLES
from barber_common.auth import Principal, require_roles
from barber_common.idempotency import RequiredIdempotencyKey

__all__ = ["router"]

router = APIRouter(prefix="/bookings", tags=["bookings"])

# Everyone who registers holds this role, so the check does not narrow who may
# book; it is what keeps the endpoint closed to a service token.
Client = Annotated[Principal, Depends(require_roles("client"))]
# Anyone a booking may concern. Which booking, and in what capacity, is checked
# by the scenario once the booking is loaded.
Participant = Annotated[Principal, Depends(require_roles(*BOOKING_ROLES))]

_REFUSED: dict[int | str, dict[str, object]] = {
    status.HTTP_403_FORBIDDEN: {
        "description": "The booking is not the caller's, or their role does not allow this"
    },
    status.HTTP_404_NOT_FOUND: {"description": "No such booking"},
}


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


@router.post(
    "/{booking_id}/cancel",
    summary="Cancel a booking",
    responses={
        **_REFUSED,
        status.HTTP_409_CONFLICT: {
            "description": "The visit is already completed or marked as a no-show"
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "The client's deadline has passed, or the visit has already started"
        },
    },
)
async def cancel_booking(
    booking_id: UUID,
    caller: Participant,
    scenario: CancelBookingScenario,
    body: Annotated[BookingCancelRequest | None, Body()] = None,
) -> BookingResponse:
    """Call a visit off and free its time at once.

    The client of the booking cancels it up to the salon's deadline before the
    start -- the deadline as it was when the booking was made. Later than that
    is `cancel_deadline_passed`, and only the salon can still cancel: a
    `salon_admin` of the salon or a `super_admin`, whose cancellation is
    recorded as the salon's. The master of the booking cannot cancel it.

    Nobody cancels a visit that has started (`booking_already_started`), and a
    completed visit or a no-show is `409 booking_status_conflict`.

    Cancelling a cancelled booking answers with it as it is, and nothing is
    sent to the client a second time.
    """
    return await scenario.execute(caller=caller, booking_id=BookingId(booking_id), body=body)


@router.post(
    "/{booking_id}/reschedule",
    summary="Move a booking to another time",
    responses={
        **_REFUSED,
        status.HTTP_409_CONFLICT: {
            "description": (
                "The new time was taken while this request was in flight, or the booking "
                "is no longer open"
            )
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": (
                "The client's deadline has passed, the visit has started, or the new time "
                "breaks one of the salon's windows"
            )
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "The catalog is not answering"},
    },
)
async def reschedule_booking(
    booking_id: UUID,
    body: BookingRescheduleRequest,
    caller: Participant,
    scenario: RescheduleBookingScenario,
) -> BookingResponse:
    """Move a booking to another start, atomically.

    The same booking at a new time: not a cancellation and a new booking. If
    the new time is lost to somebody else, the answer is `409 slot_taken` with
    `alternatives`, and the booking stays where it was.

    Who may, and until when, is the same as for a cancellation: the client up
    to the salon's deadline before the current start, the salon at any moment
    before it. The new time passes the same checks as a new booking --
    `booking_too_late`, `booking_too_far`, `slot_outside_schedule`,
    `master_inactive`, `service_not_offered`. The service, its price and its
    duration stay as they were booked.

    Moving a booking to the start it already has answers with it as it is.
    """
    return await scenario.execute(caller=caller, booking_id=BookingId(booking_id), body=body)
