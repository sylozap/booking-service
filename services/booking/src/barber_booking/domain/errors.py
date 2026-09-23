"""Failures the booking service can report.

Each error carries its domain ``code``, and the chassis handler turns it into
``problem+json``. This is the one module of the domain that imports the
chassis, and only for the error base.
"""

from __future__ import annotations

from barber_common.errors import DomainError

__all__ = [
    "BookingTooFar",
    "BookingTooLate",
    "ConflictingExceptions",
    "DateInThePast",
    "MasterInactive",
    "MasterNotFound",
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
