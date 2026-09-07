"""The password policy and the port a hasher has to satisfy.

The policy is a pure function: it decides whether a password may be accepted
and says nothing about how it is stored. The storage is behind
:class:`PasswordHasher`, a protocol -- the domain must not depend on argon2,
and a scenario must not be able to reach around the port and store a password
in any other form (docs/CODING_STANDARDS.md sections 2.2 and 4).

The password never leaves this module in a message, a log record or an
exception: :class:`~barber_auth.domain.errors.WeakPassword` names the rule that
failed, not the value that failed it.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from barber_auth.domain.errors import WeakPassword

__all__ = [
    "MAX_PASSWORD_LENGTH",
    "MIN_PASSWORD_LENGTH",
    "PasswordHasher",
    "check_password_policy",
]

MIN_PASSWORD_LENGTH = 10
# Not a security limit but a cost limit: argon2 hashes whatever it is given,
# and a megabyte of password is a way to spend the CPU of the service.
MAX_PASSWORD_LENGTH = 256


@runtime_checkable
class PasswordHasher(Protocol):
    """What the scenarios need from a password hash, and nothing more.

    Runtime checkable so the composition root can assert that what it put in
    the application state really satisfies the port; the check is structural
    and costs one lookup at startup.
    """

    def hash(self, password: str) -> str:
        """Return a self-describing hash: algorithm, parameters, salt, digest."""
        ...

    def verify(self, password: str, password_hash: str) -> bool:
        """Report whether the password produced this hash."""
        ...

    def verify_dummy(self) -> None:
        """Spend what a real verification spends, against a hash of its own.

        Called when there is no user to verify against. Without it, "no such
        user" answers in microseconds and "wrong password" in the cost of an
        argon2 pass, and the difference enumerates the accounts of the platform.
        """
        ...


def check_password_policy(password: str, *, min_length: int = MIN_PASSWORD_LENGTH) -> None:
    """Accept the password, or say which rule it broke.

    Length carries most of the strength, so it is the rule with a number behind
    it; the character classes only rule out the passwords that are long and
    still trivial -- ``aaaaaaaaaaaa`` and ``000000000000``.
    """
    if len(password) < min_length:
        raise WeakPassword(f"Password must be at least {min_length} characters long")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise WeakPassword(f"Password must be at most {MAX_PASSWORD_LENGTH} characters long")
    if password.strip() != password or not password.strip():
        raise WeakPassword("Password must not start or end with whitespace")
    if not any(character.isalpha() for character in password):
        raise WeakPassword("Password must contain at least one letter")
    if not any(character.isdigit() for character in password):
        raise WeakPassword("Password must contain at least one digit")
