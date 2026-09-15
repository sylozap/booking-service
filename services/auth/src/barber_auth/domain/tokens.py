"""Rules of the token pair: access token claims and refresh token validity.

Pure functions with the clock passed in. The access token is a signed statement
verified locally by every service, so it cannot be revoked and has a short TTL.
The refresh token is an opaque secret stored as a hash and rotated on every
use.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from uuid import UUID

from barber_auth.domain.identifiers import UserId
from barber_auth.domain.roles import RoleGrant

__all__ = [
    "ACCESS_TOKEN_TTL_MINUTES",
    "ACCESS_TOKEN_TYPE",
    "REFRESH_TOKEN_BYTES",
    "REFRESH_TOKEN_TTL_DAYS",
    "SERVICE_TOKEN_TTL_MINUTES",
    "SERVICE_TOKEN_TYPE",
    "build_access_claims",
    "build_service_claims",
    "hash_refresh_token",
    "is_refresh_token_reused",
    "is_refresh_token_usable",
]

# Access tokens cannot be revoked, so they expire quickly.
ACCESS_TOKEN_TTL_MINUTES = 15
REFRESH_TOKEN_TTL_DAYS = 30

# 256 bits from ``secrets``. The token has no structure and is never derived
# from anything, so guessing is the only attack and this is the answer to it.
REFRESH_TOKEN_BYTES = 32

# The ``typ`` claim of a user access token, which tells it apart from a service
# token.
ACCESS_TOKEN_TYPE = "access"  # noqa: S105 - the name of a token kind, not a credential
# The ``typ`` claim of a service token, required by endpoints under /internal.
SERVICE_TOKEN_TYPE = "service"  # noqa: S105 - likewise a kind, not a credential

# Shorter than a user token on purpose. A service asks for one per burst of
# calls rather than per session, so a short life costs little and bounds the
# window a leaked one is useful for.
SERVICE_TOKEN_TTL_MINUTES = 5


def build_access_claims(
    *,
    user_id: UserId,
    roles: tuple[RoleGrant, ...],
    issuer: str,
    token_id: UUID,
    issued_at: datetime,
    ttl_minutes: int = ACCESS_TOKEN_TTL_MINUTES,
) -> dict[str, object]:
    """Assemble the payload of an access token.

    ``iat`` and ``exp`` are integer seconds since the epoch (RFC 7519
    NumericDate), computed from ``issued_at``.
    """
    expires_at = issued_at + timedelta(minutes=ttl_minutes)
    return {
        "sub": str(user_id),
        "roles": [grant.as_claim() for grant in roles],
        "iss": issuer,
        # Identifies this exact token. Nothing in the platform looks it up --
        # access tokens are not tracked -- but it is what makes two otherwise
        # identical tokens distinguishable in a log or an incident.
        "jti": str(token_id),
        "typ": ACCESS_TOKEN_TYPE,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }


def build_service_claims(
    *,
    client_id: str,
    scopes: tuple[str, ...],
    issuer: str,
    token_id: UUID,
    issued_at: datetime,
    ttl_minutes: int = SERVICE_TOKEN_TTL_MINUTES,
) -> dict[str, object]:
    """Assemble the payload of a machine-to-machine token.

    ``sub`` is the client id, ``typ`` is ``service``, and permissions are
    ``scopes``; there is no ``roles`` claim.
    """
    expires_at = issued_at + timedelta(minutes=ttl_minutes)
    return {
        "sub": client_id,
        "scopes": list(scopes),
        "iss": issuer,
        "jti": str(token_id),
        "typ": SERVICE_TOKEN_TYPE,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }


def hash_refresh_token(token: str) -> str:
    """Return the value stored in ``refresh_tokens.token_hash``.

    SHA-256 is enough because the token is 32 random bytes.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_refresh_token_usable(
    *,
    revoked_at: datetime | None,
    expires_at: datetime,
    now: datetime,
) -> bool:
    """Whether this refresh token may still be exchanged for a new pair.

    Reuse detection is separate, in :func:`is_refresh_token_reused`.
    """
    return revoked_at is None and expires_at > now


def is_refresh_token_reused(*, revoked_at: datetime | None) -> bool:
    """Whether presenting this token means someone kept a copy of it.

    True for a revoked token presented again; the caller then revokes the whole
    family. An expired token is not reuse.
    """
    return revoked_at is not None
