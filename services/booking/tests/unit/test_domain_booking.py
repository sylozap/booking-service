"""A booking as the domain holds it."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from barber_booking.domain.booking import Actor, Booking, ServiceSnapshot
from barber_booking.domain.booking_status import ACTIVE_STATUSES, BookingStatus
from barber_booking.domain.errors import (
    BookingAlreadyStarted,
    BookingNotStarted,
    BookingStatusConflict,
    CancelDeadlinePassed,
    NotAllowedForActor,
)
from barber_booking.domain.identifiers import BookingId, MasterId, SalonId, ServiceId, UserId
from barber_booking.domain.time_range import TimeRange

START = datetime(2026, 10, 5, 7, 0, tzinfo=UTC)
CLIENT = UserId(uuid4())
ADMIN = UserId(uuid4())

AS_CLIENT = Actor(user_id=CLIENT, is_client=True)
AS_SALON = Actor(user_id=ADMIN, runs_salon=True)
AS_MASTER = Actor(user_id=UserId(uuid4()), is_master=True)
AS_STRANGER = Actor(user_id=UserId(uuid4()))


def a_booking(
    *,
    start_at: datetime = START,
    buffer_min: int = 0,
    cancel_deadline_min: int = 240,
) -> Booking:
    """A confirmed 45-minute booking; a test bends the fields it is about."""
    return Booking.confirmed(
        id=BookingId(uuid4()),
        salon_id=SalonId(uuid4()),
        master_id=MasterId(uuid4()),
        client_user_id=CLIENT,
        service=ServiceSnapshot(
            service_id=ServiceId(uuid4()),
            name="Haircut",
            price=Decimal("3500.00"),
            currency="RUB",
            duration_min=45,
        ),
        buffer_min=buffer_min,
        cancel_deadline_min=cancel_deadline_min,
        start_at=start_at,
        reminder_at=None,
    )


def test_a_new_booking_is_confirmed_at_once_and_made_by_its_client() -> None:
    booking = a_booking()

    assert booking.status is BookingStatus.CONFIRMED
    assert booking.created_by == CLIENT


def test_a_booking_ends_when_its_service_does() -> None:
    booking = a_booking(buffer_min=15)

    assert booking.end_at == START + timedelta(minutes=45)


def test_the_buffer_keeps_the_master_occupied_after_the_service() -> None:
    booking = a_booking(buffer_min=15)

    assert booking.occupied == TimeRange(START, START + timedelta(minutes=60))


def test_a_booking_without_a_time_zone_is_refused() -> None:
    with pytest.raises(ValueError, match="time zone"):
        a_booking(start_at=START.replace(tzinfo=None))


def test_a_negative_buffer_is_refused() -> None:
    with pytest.raises(ValueError, match="buffer"):
        a_booking(buffer_min=-5)


def test_a_negative_cancellation_deadline_is_refused() -> None:
    with pytest.raises(ValueError, match="deadline"):
        a_booking(cancel_deadline_min=-1)


def test_a_service_without_a_duration_is_refused() -> None:
    with pytest.raises(ValueError, match="duration"):
        replace(a_booking().service, duration_min=0)


# --- cancelling -------------------------------------------------------------


def hours_before(hours: float) -> datetime:
    return START - timedelta(hours=hours)


def test_the_client_cancels_before_the_deadline() -> None:
    cancelled = a_booking().cancel(by=AS_CLIENT, now=hours_before(5), reason="changed plans")

    assert cancelled.status is BookingStatus.CANCELLED_BY_CLIENT
    assert cancelled.cancelled_by == CLIENT
    assert cancelled.cancelled_at == hours_before(5)
    assert cancelled.cancel_reason == "changed plans"


def test_the_client_may_still_cancel_exactly_at_the_deadline() -> None:
    cancelled = a_booking(cancel_deadline_min=240).cancel(by=AS_CLIENT, now=hours_before(4))

    assert cancelled.status is BookingStatus.CANCELLED_BY_CLIENT


def test_the_client_cannot_cancel_after_the_deadline() -> None:
    with pytest.raises(CancelDeadlinePassed) as failure:
        a_booking(cancel_deadline_min=240).cancel(by=AS_CLIENT, now=hours_before(3))

    assert failure.value.code == "cancel_deadline_passed"


def test_the_salon_cancels_after_the_deadline() -> None:
    cancelled = a_booking().cancel(by=AS_SALON, now=hours_before(1))

    assert cancelled.status is BookingStatus.CANCELLED_BY_SALON
    assert cancelled.cancelled_by == ADMIN


def test_an_admin_cancelling_their_own_booking_is_its_client_without_the_deadline() -> None:
    both = Actor(user_id=CLIENT, is_client=True, runs_salon=True)

    cancelled = a_booking().cancel(by=both, now=hours_before(1))

    assert cancelled.status is BookingStatus.CANCELLED_BY_CLIENT


@pytest.mark.parametrize("actor", [AS_MASTER, AS_STRANGER])
def test_nobody_else_may_cancel(actor: Actor) -> None:
    with pytest.raises(NotAllowedForActor):
        a_booking().cancel(by=actor, now=hours_before(5))


def test_nobody_cancels_a_visit_that_has_started() -> None:
    with pytest.raises(BookingAlreadyStarted):
        a_booking().cancel(by=AS_SALON, now=START)


def test_cancelling_again_changes_nothing() -> None:
    cancelled = a_booking().cancel(by=AS_CLIENT, now=hours_before(5))

    again = cancelled.cancel(by=AS_CLIENT, now=hours_before(4.5))

    assert again is cancelled


@pytest.mark.parametrize("status", [BookingStatus.COMPLETED, BookingStatus.NO_SHOW])
def test_a_closed_visit_cannot_be_cancelled(status: BookingStatus) -> None:
    closed = replace(a_booking(), status=status)

    with pytest.raises(BookingStatusConflict):
        closed.cancel(by=AS_SALON, now=START + timedelta(hours=2))


# --- moving -----------------------------------------------------------------

LATER = START + timedelta(hours=2)


def move(booking: Booking, *, by: Actor = AS_CLIENT, now: datetime | None = None) -> Booking:
    return booking.reschedule(
        by=by,
        now=now or hours_before(24),
        start_at=LATER,
        buffer_min=10,
        reminder_at=LATER - timedelta(hours=4),
    )


def test_a_moved_booking_keeps_its_identity_and_its_snapshot() -> None:
    booking = a_booking()

    moved = move(booking)

    assert moved.id == booking.id
    assert moved.service == booking.service
    assert moved.start_at == LATER
    assert moved.end_at == LATER + timedelta(minutes=45)


def test_a_moved_booking_takes_the_current_buffer_of_the_master() -> None:
    moved = move(a_booking(buffer_min=0))

    assert moved.buffer_min == 10


def test_the_reminder_is_due_anew_after_a_move() -> None:
    reminded = replace(a_booking(), reminder_sent_at=hours_before(4))

    moved = move(reminded)

    assert moved.reminder_at == LATER - timedelta(hours=4)
    assert moved.reminder_sent_at is None


def test_the_client_cannot_move_after_the_deadline() -> None:
    with pytest.raises(CancelDeadlinePassed):
        move(a_booking(), now=hours_before(3))


def test_the_salon_moves_after_the_deadline() -> None:
    moved = move(a_booking(), by=AS_SALON, now=hours_before(1))

    assert moved.start_at == LATER


@pytest.mark.parametrize("actor", [AS_MASTER, AS_STRANGER])
def test_nobody_else_may_move(actor: Actor) -> None:
    with pytest.raises(NotAllowedForActor):
        move(a_booking(), by=actor)


def test_nobody_moves_a_visit_that_has_started() -> None:
    with pytest.raises(BookingAlreadyStarted):
        move(a_booking(), by=AS_SALON, now=START + timedelta(minutes=5))


def test_a_cancelled_booking_cannot_be_moved() -> None:
    cancelled = a_booking().cancel(by=AS_CLIENT, now=hours_before(24))

    with pytest.raises(BookingStatusConflict):
        move(cancelled)


def test_moving_to_where_the_booking_already_is_changes_nothing() -> None:
    moved = move(a_booking())

    # Two hours before the new start: past the deadline, yet a repeat of the
    # move that already happened is not a new move.
    again = move(moved, now=LATER - timedelta(hours=2))

    assert again is moved


# --- closing a visit --------------------------------------------------------

AFTER_START = START + timedelta(minutes=10)
OUTCOMES = [BookingStatus.COMPLETED, BookingStatus.NO_SHOW]


@pytest.mark.parametrize("outcome", OUTCOMES)
@pytest.mark.parametrize("actor", [AS_MASTER, AS_SALON])
def test_the_master_or_the_salon_closes_a_visit_that_has_started(
    outcome: BookingStatus, actor: Actor
) -> None:
    closed = a_booking().close(outcome=outcome, by=actor, now=AFTER_START)

    assert closed.status is outcome


def test_a_visit_is_closed_from_the_moment_it_starts() -> None:
    closed = a_booking().close(outcome=BookingStatus.COMPLETED, by=AS_MASTER, now=START)

    assert closed.status is BookingStatus.COMPLETED


def test_a_visit_still_ahead_cannot_be_closed() -> None:
    with pytest.raises(BookingNotStarted) as failure:
        a_booking().close(outcome=BookingStatus.COMPLETED, by=AS_MASTER, now=hours_before(1))

    assert failure.value.code == "booking_not_started"


@pytest.mark.parametrize("actor", [AS_CLIENT, AS_STRANGER])
def test_the_client_does_not_close_their_own_visit(actor: Actor) -> None:
    with pytest.raises(NotAllowedForActor):
        a_booking().close(outcome=BookingStatus.COMPLETED, by=actor, now=AFTER_START)


def test_closing_with_the_same_outcome_again_changes_nothing() -> None:
    closed = a_booking().close(outcome=BookingStatus.NO_SHOW, by=AS_MASTER, now=AFTER_START)

    again = closed.close(outcome=BookingStatus.NO_SHOW, by=AS_SALON, now=AFTER_START)

    assert again is closed


def test_a_completed_visit_is_not_turned_into_a_no_show() -> None:
    closed = a_booking().close(outcome=BookingStatus.COMPLETED, by=AS_MASTER, now=AFTER_START)

    with pytest.raises(BookingStatusConflict):
        closed.close(outcome=BookingStatus.NO_SHOW, by=AS_MASTER, now=AFTER_START)


def test_a_cancelled_visit_cannot_be_closed() -> None:
    cancelled = a_booking().cancel(by=AS_CLIENT, now=hours_before(24))

    with pytest.raises(BookingStatusConflict):
        cancelled.close(outcome=BookingStatus.COMPLETED, by=AS_SALON, now=AFTER_START)


def test_a_closed_visit_still_holds_its_time() -> None:
    # Both outcomes stay in the exclusion constraint: the past is not bookable.
    assert all(outcome in ACTIVE_STATUSES for outcome in OUTCOMES)


def test_only_an_outcome_of_a_visit_closes_it() -> None:
    with pytest.raises(ValueError, match="how a visit ends"):
        a_booking().close(outcome=BookingStatus.CANCELLED_BY_SALON, by=AS_SALON, now=AFTER_START)


# --- the salon on its own ---------------------------------------------------


def test_the_salon_on_its_own_cancels_with_nobody_recorded() -> None:
    cancelled = a_booking().cancel(by=Actor.the_salon(), now=hours_before(1), reason="gone")

    assert cancelled.status is BookingStatus.CANCELLED_BY_SALON
    assert cancelled.cancelled_by is None
    assert cancelled.cancel_reason == "gone"
