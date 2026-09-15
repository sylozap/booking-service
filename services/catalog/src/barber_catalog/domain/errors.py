"""Failures the catalog can report.

Each error carries its domain ``code``, and the chassis handler turns it into
``problem+json``. A caller who may not touch a salon gets ``403`` whatever the
reason; ``404`` is answered only to a caller entitled to see the entity.
"""

from __future__ import annotations

from barber_common.errors import DomainError

__all__ = [
    "MasterNotFound",
    "MasterProfileExists",
    "MasterServiceNotFound",
    "SalonNotFound",
    "ServiceArchived",
    "ServiceFromAnotherSalon",
    "ServiceNotFound",
]


class SalonNotFound(DomainError):
    """No salon with this identifier."""

    code = "not_found"
    http_status = 404
    title = "Salon not found"


class MasterNotFound(DomainError):
    """No master profile with this identifier."""

    code = "not_found"
    http_status = 404
    title = "Master not found"


class ServiceNotFound(DomainError):
    """No service with this identifier.

    An archived service is still found: bookings refer to it, and a history
    that cannot name what was booked is a history nobody can read.
    """

    code = "not_found"
    http_status = 404
    title = "Service not found"


class MasterProfileExists(DomainError):
    """This account already has a profile in this salon."""

    code = "master_profile_exists"
    http_status = 409
    title = "This account already has a profile in this salon"


class MasterServiceNotFound(DomainError):
    """This master does not offer this service."""

    code = "not_found"
    http_status = 404
    title = "Master does not offer this service"


class ServiceFromAnotherSalon(DomainError):
    """The service belongs to a different salon than the master.

    ``422`` rather than ``404``: both entities exist and the caller may see
    both, so the failure is about the combination, not about visibility.
    """

    code = "validation_error"
    http_status = 422
    title = "Service belongs to another salon"


class ServiceArchived(DomainError):
    """The service is archived and cannot be taken up by a master.

    Archiving is how a salon stops offering something; letting a master pick it
    up again afterwards would make the archive an opinion rather than a state.
    """

    code = "validation_error"
    http_status = 422
    title = "Service is archived"
