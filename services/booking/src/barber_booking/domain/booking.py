"""A booking as the rules see it, and the ways it may change.

Immutable: a change returns a new booking, and the scenario hands that to the
repository. Whether the new time is free is never decided here -- that is the
exclusion constraint's answer at the moment of the write.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal

from barber_booking.domain.booking_status import CANCELLED_STATUSES, OPEN_STATUSES, BookingStatus
from barber_booking.domain.errors import (
    BookingAlreadyStarted,
    BookingNotStarted,
    BookingStatusConflict,
    CancelDeadlinePassed,
    NotAllowedForActor,
)
from barber_booking.domain.identifiers import BookingId, MasterId, SalonId, ServiceId, UserId
from barber_booking.domain.time_range import TimeRange

__all__ = [
    "MASTER_DEACTIVATED",
    "VISIT_OUTCOMES",
    "Actor",
    "Booking",
    "ServiceSnapshot",
    "reminder_for",
]

# The reason a cascade writes on every booking it cancels, and sends on.
MASTER_DEACTIVATED = "master_deactivated"

# What a visit that has started can turn out to be. Both keep the time taken:
# the exclusion constraint counts them, so the past never becomes bookable.
VISIT_OUTCOMES = frozenset({BookingStatus.COMPLETED, BookingStatus.NO_SHOW})


def reminder_for(start_at: datetime, *, now: datetime, lead: timedelta) -> datetime | None:
    """When to remind the client of a visit, unless that moment has already passed."""
    reminder_at = start_at - lead
    return reminder_at if reminder_at > now else None


@dataclass(frozen=True, slots=True)
class ServiceSnapshot:
    """The service as it was when it was booked.

    A later change of the price list does not reach a booking that already
    exists: the client keeps what they were told.
    """

    service_id: ServiceId
    name: str
    price: Decimal
    currency: str
    duration_min: int

    def __post_init__(self) -> None:
        if self.duration_min <= 0:
            raise ValueError("the duration of a service has to be positive")


@dataclass(frozen=True, slots=True)
class Actor:
    """Who is acting on a booking, in the capacities that matter to it.

    One person may hold several: an admin who booked a haircut in their own
    salon is its client and runs the salon at once.
    """

    # Nobody for the platform acting on its own, as when a master leaves.
    user_id: UserId | None
    # The client of this booking.
    is_client: bool = False
    # A salon_admin of its salon, or a super_admin.
    runs_salon: bool = False
    # The master the booking is with.
    is_master: bool = False

    @classmethod
    def the_salon(cls) -> Actor:
        """The salon acting with nobody behind the request.

        A cascade started by an event has the powers of the salon and no
        person to record as the one who cancelled.
        """
        return cls(user_id=None, runs_salon=True)


@dataclass(frozen=True, slots=True)
class Booking:
    """One client, one master, one service, one span of time."""

    id: BookingId
    salon_id: SalonId
    master_id: MasterId
    client_user_id: UserId
    service: ServiceSnapshot
    buffer_min: int
    # The salon's policy as it stood when this was booked.
    cancel_deadline_min: int

    start_at: datetime
    status: BookingStatus
    created_by: UserId

    cancelled_by: UserId | None = None
    cancel_reason: str | None = None
    cancelled_at: datetime | None = None

    reminder_at: datetime | None = None
    reminder_sent_at: datetime | None = None

    # Given by the database; absent until the booking is stored.
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.start_at.tzinfo is None:
            raise ValueError("a booking starts at an instant with a time zone")
        if self.buffer_min < 0:
            raise ValueError("the buffer cannot be negative")
        if self.cancel_deadline_min < 0:
            raise ValueError("the cancellation deadline cannot be negative")

    @classmethod
    def confirmed(
        cls,
        *,
        id: BookingId,
        salon_id: SalonId,
        master_id: MasterId,
        client_user_id: UserId,
        service: ServiceSnapshot,
        buffer_min: int,
        cancel_deadline_min: int,
        start_at: datetime,
        reminder_at: datetime | None,
        by: Actor,
    ) -> Booking:
        """A new booking, confirmed at once, made by ``by``.

        A client books for themselves. Booking for somebody else -- a client
        who called or walked in -- is the salon's: its admin puts the visit on
        that client's account, and ``created_by`` remembers who did.

        ``pending`` exists for a prepayment step the platform does not have
        yet, so nothing waits in it today.
        """
        if by.user_id is None:
            raise ValueError("a booking is made by somebody")
        if by.user_id != client_user_id and not by.runs_salon:
            raise NotAllowedForActor("Only the salon may book on behalf of another client")
        return cls(
            id=id,
            salon_id=salon_id,
            master_id=master_id,
            client_user_id=client_user_id,
            service=service,
            buffer_min=buffer_min,
            cancel_deadline_min=cancel_deadline_min,
            start_at=start_at,
            status=BookingStatus.CONFIRMED,
            created_by=by.user_id,
            reminder_at=reminder_at,
        )

    @property
    def duration_min(self) -> int:
        return self.service.duration_min

    @property
    def end_at(self) -> datetime:
        """When the service ends. The buffer after it is not part of the visit."""
        return self.start_at + timedelta(minutes=self.service.duration_min)

    @property
    def occupied(self) -> TimeRange:
        """The time the master is kept for: the service and the buffer after it."""
        return TimeRange(self.start_at, self.end_at + timedelta(minutes=self.buffer_min))

    @property
    def is_cancelled(self) -> bool:
        return self.status in CANCELLED_STATUSES

    def has_started(self, now: datetime) -> bool:
        return now >= self.start_at

    def cancel(self, *, by: Actor, now: datetime, reason: str | None = None) -> Booking:
        """Call the visit off and free its time.

        The client cancels up to ``cancel_deadline_min`` before the start; the
        salon at any moment before it. Recorded as the client's cancellation
        when the client does it, even if they also run the salon -- the status
        says who changed their mind, not who had the power to.

        Cancelling a cancelled booking returns it as it is: a repeated request
        has nothing left to do, and must not look like a failure.

        The master of the booking is not among those who may: a master who
        cannot come takes a day off, and the salon decides about the clients.
        """
        if not (by.is_client or by.runs_salon):
            raise NotAllowedForActor("Only the client or the salon may cancel a booking")
        if self.is_cancelled:
            return self
        if self.status not in OPEN_STATUSES:
            raise BookingStatusConflict(f"A booking that is {self.status} cannot be cancelled")
        if self.has_started(now):
            raise BookingAlreadyStarted("The visit has already started")
        if not by.runs_salon and self._deadline_passed(now):
            raise CancelDeadlinePassed(
                f"Cancelling later than {self.cancel_deadline_min} minutes before the start "
                "is up to the salon"
            )

        status = (
            BookingStatus.CANCELLED_BY_CLIENT if by.is_client else BookingStatus.CANCELLED_BY_SALON
        )
        return replace(
            self,
            status=status,
            cancelled_by=by.user_id,
            cancelled_at=now,
            cancel_reason=reason,
        )

    def check_move(self, *, by: Actor, now: datetime, to: datetime) -> bool:
        """Refuse a move this actor may not make now; say whether there is one to make.

        The same people as for a cancellation, under the same deadline counted
        from the current start: moving a visit is calling off its time. A
        booking already at ``to`` has nothing to do -- a repeated request finds
        the work done -- and that is answered before the deadline, which the
        move itself may have brought closer.
        """
        if not (by.is_client or by.runs_salon):
            raise NotAllowedForActor("Only the client or the salon may move a booking")
        if self.status not in OPEN_STATUSES:
            raise BookingStatusConflict(f"A booking that is {self.status} cannot be moved")
        if to == self.start_at:
            return False
        if self.has_started(now):
            raise BookingAlreadyStarted("The visit has already started")
        if not by.runs_salon and self._deadline_passed(now):
            raise CancelDeadlinePassed(
                f"Moving later than {self.cancel_deadline_min} minutes before the start "
                "is up to the salon"
            )
        return True

    def reschedule(
        self,
        *,
        by: Actor,
        now: datetime,
        start_at: datetime,
        buffer_min: int,
        reminder_at: datetime | None,
    ) -> Booking:
        """The same booking at another time.

        The service keeps its snapshot -- name, price and duration are what the
        client bought. The buffer is the master's current one, because it is
        the master's time it protects. The reminder is due anew, even if the
        old one was already sent.
        """
        if not self.check_move(by=by, now=now, to=start_at):
            return self
        return replace(
            self,
            start_at=start_at,
            buffer_min=buffer_min,
            reminder_at=reminder_at,
            reminder_sent_at=None,
        )

    def close(self, *, outcome: BookingStatus, by: Actor, now: datetime) -> Booking:
        """Record how a visit went: the client came, or did not.

        The master of the booking says so, or the salon; the client does not
        mark their own visit. Only once it has started, and only once: a visit
        recorded as completed is not later turned into a no-show. Recording the
        same outcome again returns the booking as it is.
        """
        if outcome not in VISIT_OUTCOMES:
            raise ValueError(f"{outcome} is not how a visit ends")
        if not (by.is_master or by.runs_salon):
            raise NotAllowedForActor("Only the master or the salon may close a visit")
        if self.status is outcome:
            return self
        if self.status not in OPEN_STATUSES:
            raise BookingStatusConflict(f"A booking that is {self.status} cannot be closed")
        if not self.has_started(now):
            raise BookingNotStarted("The visit has not started yet")
        return replace(self, status=outcome)

    def _deadline_passed(self, now: datetime) -> bool:
        """Whether the client's own window for changes has closed.

        Exactly at the deadline it is still open: "four hours before" includes
        the moment four hours before.
        """
        return self.start_at - now < timedelta(minutes=self.cancel_deadline_min)
