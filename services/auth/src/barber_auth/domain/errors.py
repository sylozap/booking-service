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


class InvalidCredentials(DomainError):
    """The login attempt failed, and the answer does not say why.

    One error for "no such user", "wrong password" and "the account is
    deactivated". Three different answers would turn the login endpoint into a
    way to find out which addresses are registered, and the timing is equalised
    for the same reason -- see
    :meth:`~barber_auth.domain.passwords.PasswordHasher.verify_dummy`.

    Deliberately not ``403``: the caller may retry with other credentials, and
    ``401`` is what says so.
    """

    code = "unauthorized"
    http_status = 401
    title = "Authentication required"


class EmailNotConfirmed(DomainError):
    """The password was right, but the address behind the account is unproven.

    This one does name its reason, and it is the exception to the rule above on
    purpose: the caller has already proven they know the password, so nothing
    is being revealed to a stranger, and a user who cannot be told to go and
    click the link has no way out of the state they are in.
    """

    code = "email_not_confirmed"
    http_status = 403
    title = "Email address is not confirmed"


class RefreshTokenInvalid(DomainError):
    """The refresh token is unknown, expired, or already spent.

    One code for all three, and the reuse of an already spent token is not
    distinguished either -- it answers exactly like an expired one, while
    revoking the whole family behind the caller's back. Naming it would tell
    whoever stole the token that the theft was noticed.
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
