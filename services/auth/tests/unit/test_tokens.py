"""The rules behind a token pair, with no database and no key in sight."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from barber_auth.domain.identifiers import SalonId, UserId
from barber_auth.domain.roles import Role
from barber_auth.domain.tokens import (
    ACCESS_TOKEN_TTL_MINUTES,
    RoleGrant,
    build_access_claims,
    hash_refresh_token,
    is_refresh_token_reused,
    is_refresh_token_usable,
)

ISSUER = "https://barber.local/auth"
NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def claims(**overrides: object) -> dict[str, object]:
    """Access claims with everything but the field under test defaulted."""
    fields: dict[str, object] = {
        "user_id": UserId(uuid4()),
        "roles": (RoleGrant(Role.CLIENT),),
        "issuer": ISSUER,
        "token_id": uuid4(),
        "issued_at": NOW,
    }
    fields.update(overrides)
    return build_access_claims(**fields)  # type: ignore[arg-type]  # keyword types differ per key


def test_the_subject_is_the_user() -> None:
    user_id = UserId(uuid4())

    assert claims(user_id=user_id)["sub"] == str(user_id)


def test_the_token_expires_after_the_configured_lifetime() -> None:
    built = claims()

    assert built["exp"] == int((NOW + timedelta(minutes=ACCESS_TOKEN_TTL_MINUTES)).timestamp())
    assert built["iat"] == int(NOW.timestamp())


def test_a_shorter_lifetime_is_respected() -> None:
    built = claims(ttl_minutes=5)

    assert built["exp"] == int((NOW + timedelta(minutes=5)).timestamp())


def test_the_moments_are_integer_seconds() -> None:
    built = claims()

    # RFC 7519 calls these NumericDate. A float is accepted by some verifiers
    # and rejected by others, which is the worst of the two outcomes.
    assert isinstance(built["iat"], int)
    assert isinstance(built["exp"], int)


def test_a_global_role_carries_no_salon() -> None:
    built = claims(roles=(RoleGrant(Role.CLIENT),))

    assert built["roles"] == [{"role": "client", "salon_id": None}]


def test_a_scoped_role_names_its_salon() -> None:
    salon_id = SalonId(uuid4())

    built = claims(roles=(RoleGrant(Role.SALON_ADMIN, salon_id),))

    # This is what lets a service decide "admin of which salon" without asking
    # auth anything (docs/04-api-contracts.md).
    assert built["roles"] == [{"role": "salon_admin", "salon_id": str(salon_id)}]


def test_every_role_of_a_user_reaches_the_token() -> None:
    salon_id = SalonId(uuid4())

    built = claims(roles=(RoleGrant(Role.CLIENT), RoleGrant(Role.SALON_ADMIN, salon_id)))

    assert built["roles"] == [
        {"role": "client", "salon_id": None},
        {"role": "salon_admin", "salon_id": str(salon_id)},
    ]


def test_the_token_says_who_issued_it_and_what_it_is() -> None:
    built = claims(issuer="https://barber.dev/auth")

    assert built["iss"] == "https://barber.dev/auth"
    # T1.10 issues service tokens from the same endpoint family; typ is what
    # lets /internal refuse a user token by looking at the token.
    assert built["typ"] == "access"


def test_two_tokens_of_one_user_are_distinguishable() -> None:
    user_id = UserId(uuid4())

    first = claims(user_id=user_id)
    second = claims(user_id=user_id)

    assert first["jti"] != second["jti"]


def test_the_claims_carry_nothing_about_the_person() -> None:
    built = claims()

    # An access token is read by every service and shown in logs and traces.
    # Contacts belong to auth, and a token is not the place to spread them.
    assert set(built) == {"sub", "roles", "iss", "jti", "typ", "iat", "exp"}


def test_hashing_a_refresh_token_is_deterministic() -> None:
    assert hash_refresh_token("token-value") == hash_refresh_token("token-value")


def test_different_tokens_hash_differently() -> None:
    assert hash_refresh_token("one") != hash_refresh_token("another")


def test_the_hash_does_not_contain_the_token() -> None:
    token = "a-refresh-token-value"

    assert token not in hash_refresh_token(token)


@pytest.mark.parametrize(
    ("revoked_at", "expires_at", "usable"),
    [
        (None, NOW + timedelta(days=1), True),
        (None, NOW - timedelta(seconds=1), False),
        (NOW - timedelta(minutes=1), NOW + timedelta(days=1), False),
        (NOW - timedelta(minutes=1), NOW - timedelta(seconds=1), False),
    ],
    ids=["live", "expired", "revoked", "revoked-and-expired"],
)
def test_only_a_live_token_may_be_exchanged(
    revoked_at: datetime | None, expires_at: datetime, usable: bool
) -> None:
    assert is_refresh_token_usable(revoked_at=revoked_at, expires_at=expires_at, now=NOW) is usable


def test_a_revoked_token_presented_again_is_reuse() -> None:
    assert is_refresh_token_reused(revoked_at=NOW - timedelta(minutes=1)) is True


def test_an_expiry_is_not_reuse() -> None:
    # Otherwise coming back after a holiday would look like a stolen token and
    # would take every other session of that user down with it.
    assert is_refresh_token_reused(revoked_at=None) is False
