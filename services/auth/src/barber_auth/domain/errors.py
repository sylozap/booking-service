"""Failures the rules of auth can detect.

Each one carries the domain ``code`` from the catalogue in
``docs/04-api-contracts.md``: the router does not translate anything, it lets
the handler of the chassis turn the exception into ``problem+json``.

This is the one module of the domain that imports the chassis. Section 2.2 of
docs/CODING_STANDARDS.md keeps the domain free of ``barber_common``, and
section 6 requires domain errors to derive from ``DomainError``; the second is
the more specific rule, and the import is limited to the error base so the rest
of the domain stays copyable into an empty project.
"""

from __future__ import annotations

from barber_common.errors import DomainError

__all__ = [
    "ConfirmationTokenInvalid",
    "EmailAlreadyRegistered",
    "InvalidEmailAddress",
    "InvalidPhoneNumber",
    "PhoneAlreadyRegistered",
    "WeakPassword",
]


class InvalidEmailAddress(DomainError):
    """The string is not an email address."""

    code = "validation_error"
    http_status = 422
    title = "Email address is not valid"


class InvalidPhoneNumber(DomainError):
    """The string does not normalise to an E.164 phone number."""

    code = "validation_error"
    http_status = 422
    title = "Phone number is not valid"


class WeakPassword(DomainError):
    """The password does not satisfy the policy.

    ``detail`` says which rule failed and never quotes the password itself.
    """

    code = "validation_error"
    http_status = 422
    title = "Password is too weak"


class EmailAlreadyRegistered(DomainError):
    """Someone already registered with this email.

    Answering ``409`` tells an attacker that the address exists. Convenience
    wins here and the decision is written down in the docstring of the endpoint
    that raises it, so that changing the trade-off is a decision rather than an
    oversight.
    """

    code = "email_already_registered"
    http_status = 409
    title = "Email is already registered"


class PhoneAlreadyRegistered(DomainError):
    """Someone already registered with this phone number."""

    code = "phone_already_registered"
    http_status = 409
    title = "Phone number is already registered"


class ConfirmationTokenInvalid(DomainError):
    """The confirmation token is unknown, already used, or expired.

    One code for all three on purpose: separate answers turn the endpoint into
    an oracle that tells a caller which tokens exist and which merely expired.
    """

    code = "confirmation_token_invalid"
    http_status = 422
    title = "Confirmation token is not valid"
