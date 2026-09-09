"""Failures the catalog can report.

Each one carries the domain ``code`` from the catalogue in
``docs/04-api-contracts.md``: the router translates nothing, it lets the
handler of the chassis turn the exception into ``problem+json``.

This is the one module of the domain that imports the chassis, for the reason
given in the same place in ``auth``: section 2.2 of docs/CODING_STANDARDS.md
keeps the domain free of ``barber_common`` and section 6 requires domain errors
to derive from :class:`DomainError`; the second is the more specific rule, and
the import is limited to the error base so :mod:`pricing` next to it stays
copyable into an empty project.

**Not found and forbidden are told apart deliberately.** A caller who may not
touch a salon gets ``403`` whether the reason is their role or the salon being
someone else's, because two different answers would let a salon administrator
enumerate the salons of the platform by watching which ones answer differently
(docs/04-api-contracts.md). A missing entity answers ``404`` only once the
caller has been found entitled to see it.
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
    """This account already has a profile in this salon.

    ``409`` and not ``422``: both the account and the salon are fine, and so is
    the request -- what conflicts is the state that already exists. The caller
    almost certainly wants to edit the profile they already have.

    Not answered silently with the existing profile either, the way a repeated
    role grant is (docs/04-api-contracts.md). A grant carries nothing but
    itself, so re-granting is the same state; a profile carries a name, a bio
    and a photo, and quietly returning the old one would look like a rename
    that did not take.
    """

    code = "master_profile_exists"
    http_status = 409
    title = "This account already has a profile in this salon"


class MasterServiceNotFound(DomainError):
    """This master does not offer this service.

    Raised by the internal endpoint of T2.6, where the absence of the link is
    the answer ``booking`` needs in order to refuse the booking with
    ``service_not_offered``.
    """

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
