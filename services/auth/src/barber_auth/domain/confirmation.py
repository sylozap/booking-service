"""Hashing of email confirmation tokens.

Only the SHA-256 of a token is stored; the token itself exists only in the
email. SHA-256 is enough because the token is 32 random bytes.
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
