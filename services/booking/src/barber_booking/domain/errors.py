"""Failures the booking service can report.

Each error carries its domain ``code``, and the chassis handler turns it into
``problem+json``. This is the one module of the domain that imports the
chassis, and only for the error base.
"""

from __future__ import annotations

from barber_common.errors import DomainError

__all__ = [
    "BookingAlreadyStarted",
    "BookingNotStarted",
    "BookingStatusConflict",
    "BookingTooFar",
    "BookingTooLate",
    "CancelDeadlinePassed",
    "ConflictingExceptions",
    "DateInThePast",
    "InvalidPeriod",
    "MasterInactive",
    "MasterNotFound",
    "NotAllowedForActor",
    "OverlappingWorkingHours",
    "ServiceNotOffered",
    "SlotAlreadyTaken",
    "SlotOutsideSchedule",
]


class MasterNotFound(DomainError):
    """No settings row for this master: booking has never heard of them."""

    code = "not_found"
    http_status = 404
    title = "Master not found"


class ServiceNotOffered(DomainError):
    """This master does not offer this service, or the master does not exist."""

    code = "service_not_offered"
    http_status = 422
    title = "Master does not offer this service"


class MasterInactive(DomainError):
    """The master is deactivated and takes no new bookings."""

    code = "master_inactive"
    http_status = 422
    title = "Master is inactive"


class OverlappingWorkingHours(DomainError):
    """Two intervals of one weekday overlap: the template would say two things."""

    code = "validation_error"
    http_status = 422
    title = "Working hours overlap"


class ConflictingExceptions(DomainError):
    """Exceptions of one date contradict each other."""

    code = "validation_error"
    http_status = 422
    title = "Schedule exceptions conflict"


class DateInThePast(DomainError):
    """A schedule change for a date that has already begun changes nothing."""

    code = "validation_error"
    http_status = 422
    title = "Date is in the past"


class InvalidPeriod(DomainError):
    """A period that ends before it starts selects nothing, and is surely a mistake."""

    code = "validation_error"
    http_status = 422
    title = "Period ends before it starts"


class BookingTooLate(DomainError):
    """The start is closer than the salon's minimum notice."""

    code = "booking_too_late"
    http_status = 422
    title = "Booking is too close to its start"


class BookingTooFar(DomainError):
    """The start is further ahead than the salon's horizon."""

    code = "booking_too_far"
    http_status = 422
    title = "Booking is too far ahead"


class SlotOutsideSchedule(DomainError):
    """The master does not work then, or the start is off the grid."""

    code = "slot_outside_schedule"
    http_status = 422
    title = "Time is outside the working schedule"


class SlotAlreadyTaken(DomainError):
    """Somebody else committed first.

    ``409`` and not ``422``: the request was good, and a moment earlier it
    would have succeeded. ``alternatives`` carries the nearest free starts.
    """

    code = "slot_taken"
    http_status = 409
    title = "Slot is already taken"


class CancelDeadlinePassed(DomainError):
    """Too close to the start for the client; the salon can still do it."""

    code = "cancel_deadline_passed"
    http_status = 422
    title = "Cancellation deadline has passed"


class BookingAlreadyStarted(DomainError):
    """The visit has begun: it can be closed now, not moved or called off."""

    code = "booking_already_started"
    http_status = 422
    title = "Booking has already started"


class BookingNotStarted(DomainError):
    """The visit is still ahead: whether it happened cannot be said yet."""

    code = "booking_not_started"
    http_status = 422
    title = "Booking has not started yet"


class BookingStatusConflict(DomainError):
    """The booking is in a state this operation cannot leave.

    ``409``: the request itself is well formed, the booking has moved on --
    a completed visit cannot be cancelled, a cancelled one cannot be completed.
    """

    code = "booking_status_conflict"
    http_status = 409
    title = "Booking is not in a state that allows this"


class NotAllowedForActor(DomainError):
    """The caller may see this booking, but not do this to it.

    The same code and status as a refusal by role, whatever the reason: the
    answer does not tell a caller which capacity they were missing.
    """

    code = "forbidden_for_role"
    http_status = 403
    title = "This operation is not allowed for your role"
