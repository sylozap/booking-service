"""argon2id behind the password port.

The parameters come from the settings. Every stored hash carries the parameters
it was made with, so raising the cost keeps old hashes verifiable, and
``needs_rehash`` tells when to replace one after a successful login.
"""

from __future__ import annotations

from argon2 import PasswordHasher as Argon2PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from barber_auth.settings import AuthSettings

__all__ = ["DUMMY_PASSWORD", "Argon2Hasher"]

# Verified against when there is no user, so that the answer costs what a real
# verification costs. It is never stored and never compared to user input.
DUMMY_PASSWORD = "argon2-timing-equaliser"  # noqa: S105 - not a credential


class Argon2Hasher:
    """The production implementation of
    :class:`~barber_auth.domain.passwords.PasswordHasher`."""

    def __init__(self, settings: AuthSettings) -> None:
        self._hasher = Argon2PasswordHasher(
            time_cost=settings.password_argon2_time_cost,
            memory_cost=settings.password_argon2_memory_kib,
            parallelism=settings.password_argon2_parallelism,
            hash_len=settings.password_argon2_hash_length,
            salt_len=settings.password_argon2_salt_length,
        )
        # Built once: the point of the dummy verification is to cost the same
        # as a real one, and hashing it per call would cost twice as much.
        self._dummy_hash = self._hasher.hash(DUMMY_PASSWORD)

    def hash(self, password: str) -> str:
        """Return a new hash. Two calls on one password differ: the salt is random."""
        return self._hasher.hash(password)

    def verify(self, password: str, password_hash: str) -> bool:
        """Report whether the password produced this hash.

        A malformed hash is a mismatch, not a crash: a row damaged by a bad
        migration must not turn every login attempt into a 500.
        """
        try:
            return self._hasher.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def verify_dummy(self) -> None:
        """Spend a verification against the built-in hash.

        Called where a user was not found. Without it, an unknown email answers
        far faster than a wrong password and the difference enumerates the
        accounts of the platform.
        """
        try:
            self._hasher.verify(self._dummy_hash, DUMMY_PASSWORD)
        except VerificationError:  # pragma: no cover - the hash is built here
            return

    def needs_rehash(self, password_hash: str) -> bool:
        """Whether the hash was made with parameters weaker than the current ones."""
        try:
            return self._hasher.check_needs_rehash(password_hash)
        except InvalidHashError:
            return True
