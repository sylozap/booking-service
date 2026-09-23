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
    BookingStatusConflict,
    CancelDeadlinePassed,
    NotAllowedForActor,
)
from barber_booking.domain.identifiers import BookingId, MasterId, SalonId, ServiceId, UserId
from barber_booking.domain.time_range import TimeRange

__all__ = ["Actor", "Booking", "ServiceSnapshot"]


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

    user_id: UserId
    # The client of this booking.
    is_client: bool = False
    # A salon_admin of its salon, or a super_admin.
    runs_salon: bool = False
    # The master the booking is with.
    is_master: bool = False


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
    ) -> Booking:
        """A new booking, confirmed at once.

        ``pending`` exists for a prepayment step the platform does not have
        yet, so nothing waits in it today.
        """
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
            created_by=client_user_id,
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

    def _deadline_passed(self, now: datetime) -> bool:
        """Whether the client's own window for changes has closed.

        Exactly at the deadline it is still open: "four hours before" includes
        the moment four hours before.
        """
        return self.start_at - now < timedelta(minutes=self.cancel_deadline_min)
