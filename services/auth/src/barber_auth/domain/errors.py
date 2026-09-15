"""Failures the rules of auth can detect.

Each error carries its domain ``code``, and the chassis handler turns it into
``problem+json``. The only chassis import of the domain is the ``DomainError``
base.
"""

from __future__ import annotations

from barber_common.errors import DomainError

__all__ = [
    "ConfirmationTokenInvalid",
    "EmailAlreadyRegistered",
    "EmailNotConfirmed",
    "InvalidCredentials",
    "InvalidEmailAddress",
    "InvalidPhoneNumber",
    "PhoneAlreadyRegistered",
    "RefreshTokenInvalid",
    "RoleNotGrantable",
    "RoleNotHeld",
    "UserNotFound",
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
    """Someone already registered with this email."""

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


class InvalidCredentials(DomainError):
    """The login attempt failed, and the answer does not say why.

    Covers an unknown user, a wrong password and a deactivated account alike.
    Answered with ``401``.
    """

    code = "unauthorized"
    http_status = 401
    title = "Authentication required"


class EmailNotConfirmed(DomainError):
    """The password was right, but the email address is not confirmed yet."""

    code = "email_not_confirmed"
    http_status = 403
    title = "Email address is not confirmed"


class RefreshTokenInvalid(DomainError):
    """The refresh token is unknown, expired, or already spent.

    Reuse of a spent token answers the same way.
    """

    code = "unauthorized"
    http_status = 401
    title = "Authentication required"


class UserNotFound(DomainError):
    """No account with this identifier.

    Only raised for callers who are already entitled to manage roles, so it
    reveals nothing: an administrator who mistypes an id deserves to be told
    that rather than to watch a grant silently do nothing.
    """

    code = "not_found"
    http_status = 404
    title = "User not found"


class RoleNotGrantable(DomainError):
    """The role cannot be handed out this way.

    Either it is not grantable at all -- ``client`` arrives with registration
    and has no second source -- or it was named without the salon it only makes
    sense inside.
    """

    code = "validation_error"
    http_status = 422
    title = "Role cannot be granted"


class RoleNotHeld(DomainError):
    """The user does not hold the grant being revoked.

    Named separately from a missing user because the caller is already allowed
    to see this account, so saying so costs nothing and saves them wondering
    why nothing changed.
    """

    code = "not_found"
    http_status = 404
    title = "User does not hold this role"
