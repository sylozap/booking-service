"""Bookings: creating one, and everything that happens to it afterwards.

Creation is the only endpoint of the platform that demands
``Idempotency-Key``: a client that retries after a lost connection must not
end up with two appointments. The operations on an existing booking are
idempotent by its state -- repeating one finds the work already done.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query, status
from pydantic import AwareDatetime

from barber_booking.api.v1.dependencies import (
    CancelBookingScenario,
    CloseVisitScenario,
    CreateBookingScenario,
    ListBookingsScenario,
    ReadBookingScenario,
    RescheduleBookingScenario,
)
from barber_booking.domain.booking_status import BookingStatus
from barber_booking.domain.identifiers import BookingId, MasterId, SalonId
from barber_booking.domain.visibility import BookingFilters
from barber_booking.schemas.bookings import (
    BookingCancelRequest,
    BookingCreateRequest,
    BookingRescheduleRequest,
    BookingResponse,
)
from barber_booking.services.authorization import BOOKING_ROLES
from barber_common.auth import Principal, require_roles
from barber_common.idempotency import RequiredIdempotencyKey
from barber_common.pagination import Page, Pagination

__all__ = ["router"]

router = APIRouter(prefix="/bookings", tags=["bookings"])

# A client books for themselves, the salon for a client. Every registered user
# is a client; the check is what keeps the endpoint closed to a service token.
Booker = Annotated[Principal, Depends(require_roles("client", "salon_admin", "super_admin"))]
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
        status.HTTP_403_FORBIDDEN: {
            "description": "`client_user_id` names somebody else and the caller does not run "
            "the master's salon"
        },
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
    caller: Booker,
    request: RequiredIdempotencyKey,
    scenario: CreateBookingScenario,
) -> BookingResponse:
    """Create a booking and confirm it at once.

    The booking is the caller's own, unless `client_user_id` names another
    account: a `salon_admin` of the master's salon, or a `super_admin`, books
    a client who called or walked in. Anybody else naming another account is
    `403`. The account is not checked against the platform's users -- the
    salon takes it from the client's profile. `created_by` records who made
    the booking; the key of `Idempotency-Key` belongs to the caller.

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


@router.get(
    "",
    summary="List bookings",
    responses={
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "The period ends before it starts, or the cursor is not valid"
        }
    },
)
async def list_bookings(
    caller: Participant,
    scenario: ListBookingsScenario,
    pagination: Pagination,
    starts_from: Annotated[
        AwareDatetime | None,
        Query(alias="from", description="Bookings starting at or after this instant."),
    ] = None,
    starts_before: Annotated[
        AwareDatetime | None,
        Query(alias="to", description="Bookings starting before this instant."),
    ] = None,
    master_id: Annotated[UUID | None, Query()] = None,
    salon_id: Annotated[UUID | None, Query()] = None,
    statuses: Annotated[
        list[BookingStatus] | None,
        Query(alias="status", description="Repeat to accept several statuses."),
    ] = None,
) -> Page[BookingResponse]:
    """The bookings the caller may see, by start time, a page at a time.

    What is visible depends on who asks, not on the filters: a client sees the
    bookings they made; a master also sees every booking with them; a
    `salon_admin` sees every booking of their salon; a `super_admin` sees all.
    A master who books a haircut elsewhere sees it as its client. The filters
    narrow that set further -- a `salon_id` of another salon yields an empty
    page, not an error.

    Ordered by `start_at`, oldest first. Pages are cursor-based: pass
    `next_cursor` back as `cursor` until it is `null`.
    """
    filters = BookingFilters(
        starts_from=starts_from,
        starts_before=starts_before,
        master_id=MasterId(master_id) if master_id is not None else None,
        salon_id=SalonId(salon_id) if salon_id is not None else None,
        statuses=frozenset(statuses or ()),
    )
    return await scenario.execute(caller=caller, filters=filters, request=pagination)


@router.get(
    "/{booking_id}",
    summary="Read one booking",
    responses={status.HTTP_404_NOT_FOUND: {"description": "No such booking, or not visible"}},
)
async def read_booking(
    booking_id: UUID, caller: Participant, scenario: ReadBookingScenario
) -> BookingResponse:
    """One booking, if the caller may see it -- by the same rules as the list.

    A booking the caller may not see is `404`, exactly like one that does not
    exist: the answer says nothing about other people's appointments.
    """
    return await scenario.execute(caller=caller, booking_id=BookingId(booking_id))


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


_CLOSE_RESPONSES: dict[int | str, dict[str, object]] = {
    **_REFUSED,
    status.HTTP_409_CONFLICT: {
        "description": "The booking is cancelled, or its visit is already closed the other way"
    },
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "The visit has not started yet"},
}


@router.post(
    "/{booking_id}/complete", summary="Mark a visit as completed", responses=_CLOSE_RESPONSES
)
async def complete_visit(
    booking_id: UUID, caller: Participant, scenario: CloseVisitScenario
) -> BookingResponse:
    """Record that the client came and was served.

    For the master of the booking and for the salon -- a `salon_admin` of
    the salon or a `super_admin` -- once the visit has started
    (`booking_not_started` before that). The client cannot mark their own
    visit.

    The time stays taken: a completed visit is not free time for anybody else.
    Marking it completed again answers with it as it is; a no-show is not
    turned into a completed visit (`409 booking_status_conflict`).
    """
    return await scenario.execute(
        caller=caller, booking_id=BookingId(booking_id), outcome=BookingStatus.COMPLETED
    )


@router.post("/{booking_id}/no-show", summary="Mark a visit as missed", responses=_CLOSE_RESPONSES)
async def mark_no_show(
    booking_id: UUID, caller: Participant, scenario: CloseVisitScenario
) -> BookingResponse:
    """Record that the client did not come.

    The same people and the same rules as for completing a visit. The time
    stays taken: the master kept it free for the client, and it is in the
    past.
    """
    return await scenario.execute(
        caller=caller, booking_id=BookingId(booking_id), outcome=BookingStatus.NO_SHOW
    )
