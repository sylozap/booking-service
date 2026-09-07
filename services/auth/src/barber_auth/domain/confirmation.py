"""What the service knows about a confirmation token without holding one.

Only the hash of the token is stored, so a dump of the database does not let
anyone confirm an address they do not own. The token itself lives in the email
and nowhere else.

SHA-256 rather than argon2: the token is 32 bytes from ``secrets`` and has no
structure to guess, so there is nothing for a slow hash to protect. The value
is looked up on every confirmation request, and argon2 there would be a cost
paid for nothing. Passwords are the opposite case and use argon2.

Generating the token is not here: it needs a random source, and the domain does
not own randomness or the clock (docs/CODING_STANDARDS.md section 6).
"""

from __future__ import annotations

import hashlib
from datetime import datetime

__all__ = ["CONFIRMATION_TOKEN_BYTES", "hash_confirmation_token", "is_confirmation_usable"]

# 256 bits of entropy: brute force is not a threat model, guessing is.
CONFIRMATION_TOKEN_BYTES = 32


def hash_confirmation_token(token: str) -> str:
    """Return the value stored in ``email_confirmations.token_hash``."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_confirmation_usable(
    *,
    used_at: datetime | None,
    expires_at: datetime,
    now: datetime,
) -> bool:
    """Whether a confirmation row may still confirm an address.

    ``now`` is an argument so the rule is testable without waiting a day, and
    so that one request cannot decide expiry twice with two different clocks.
    """
    return used_at is None and expires_at > now
