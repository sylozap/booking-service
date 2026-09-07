"""The rules behind a token pair: what an access token claims, when a refresh
token still works.

Pure functions with the clock passed in. Everything that needs randomness, a
key or a database lives in the scenarios; what is here is the part that can be
tested in milliseconds and read without a container
(docs/CODING_STANDARDS.md section 6).

Two shapes of credential meet here, and they are deliberately different.

**The access token is a signed statement.** It carries who the caller is and
what roles they hold, it is verified locally by every service against the
public key from JWKS, and nothing is looked up to check it. That is what makes
it fast and what makes it impossible to revoke before it expires -- hence the
short TTL (ADR-0010).

**The refresh token is an opaque secret.** It claims nothing and means nothing
outside the row it matches. Only its hash is stored, so a dump of the database
is not a set of live sessions, and every use rotates it.
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

# docs/04-api-contracts.md. Fifteen minutes is the price of stateless
# verification: an access token cannot be withdrawn, so it has to expire
# quickly instead.
ACCESS_TOKEN_TTL_MINUTES = 15
REFRESH_TOKEN_TTL_DAYS = 30

# 256 bits from ``secrets``. The token has no structure and is never derived
# from anything, so guessing is the only attack and this is the answer to it.
REFRESH_TOKEN_BYTES = 32

# The value of the ``typ`` claim of a user access token. ``auth`` also issues
# service tokens (T1.10), and an endpoint under /internal must be able to tell
# them apart by looking at the token rather than at the shape of ``sub``.
ACCESS_TOKEN_TYPE = "access"  # noqa: S105 - the name of a token kind, not a credential
# What a service presents when it calls another service. Same signature, same
# issuer, different ``typ`` -- which is the whole of what stops a customer's
# token from opening an endpoint under /internal (T1.10).
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

    ``issued_at`` is an argument rather than a call to ``now()``: a token whose
    expiry is decided by a clock the test cannot reach is a token nobody can
    write a TTL test for, and one request must not read the time twice.

    ``iat`` and ``exp`` are integer seconds since the epoch, which is what
    RFC 7519 calls a NumericDate; a float here is accepted by some verifiers
    and rejected by others.
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

    The same shape as a user token with two differences that matter. ``sub`` is
    a client id rather than a user id, and ``typ`` says ``service`` -- so an
    endpoint under ``/internal`` refuses a customer's token by reading one
    claim rather than by guessing from the shape of the subject.

    There is no ``roles`` claim, and its absence is deliberate: a service is
    not a person and has no role in a salon. What it may do is ``scopes``, and
    the two are never mixed.
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

    SHA-256 and not argon2, for the same reason as the confirmation token: the
    value is 32 random bytes with nothing to guess, so a slow hash would buy no
    security and would be paid on every refresh. Passwords are the opposite
    case and use argon2.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_refresh_token_usable(
    *,
    revoked_at: datetime | None,
    expires_at: datetime,
    now: datetime,
) -> bool:
    """Whether this refresh token may still be exchanged for a new pair.

    A token that is not usable is not automatically a stolen one: an expired
    token is a session that ended. Telling the two apart is the job of
    :func:`is_refresh_token_reused`, and the difference decides whether the
    whole family dies.
    """
    return revoked_at is None and expires_at > now


def is_refresh_token_reused(*, revoked_at: datetime | None) -> bool:
    """Whether presenting this token means two parties hold one credential.

    A revoked row that is presented again is the signature of theft: rotation
    revokes a token at the moment it is exchanged, so the only way a revoked
    token can arrive at the endpoint is that someone kept a copy. The answer is
    to revoke the entire family, not this one row (T1.7).

    Expiry is a different thing and deliberately not part of this rule: a token
    that simply ran out is a session that ended, and killing a family over it
    would log people out for being away over the weekend.
    """
    return revoked_at is not None
