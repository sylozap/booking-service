"""Turning verified claims into something an endpoint can reason about."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from barber_common.auth.claims import (
    ACCESS_TOKEN_TYPE,
    SERVICE_TOKEN_TYPE,
    Principal,
    RoleClaim,
    principal_from_claims,
)

USER_ID = uuid4()
SALON_ID = uuid4()


def claims(**overrides: object) -> dict[str, object]:
    """A verified access token's payload, with one field under test."""
    payload: dict[str, object] = {
        "sub": str(USER_ID),
        "typ": ACCESS_TOKEN_TYPE,
        "roles": [{"role": "client", "salon_id": None}],
        "jti": str(uuid4()),
        "exp": 1788000000,
    }
    payload.update(overrides)
    return payload


def test_the_subject_becomes_the_user_id() -> None:
    principal = principal_from_claims(claims())

    assert principal.user_id == USER_ID


def test_a_service_token_has_no_user_id() -> None:
    principal = principal_from_claims(claims(typ=SERVICE_TOKEN_TYPE, sub="catalog"))

    # A client id is not a user id, and code that confuses them writes rows
    # owned by a service that has no owner.
    with pytest.raises(ValueError, match="no user id"):
        _ = principal.user_id


def test_roles_are_parsed_with_their_salon() -> None:
    principal = principal_from_claims(
        claims(roles=[{"role": "salon_admin", "salon_id": str(SALON_ID)}])
    )

    assert principal.roles == (RoleClaim(role="salon_admin", salon_id=SALON_ID),)


def test_the_expiry_is_an_aware_moment() -> None:
    principal = principal_from_claims(claims(exp=1788000000))

    assert principal.expires_at == datetime.fromtimestamp(1788000000, tz=UTC)


@pytest.mark.parametrize(
    "roles",
    [
        "not a list",
        ["not a mapping"],
        [{"salon_id": None}],
        [{"role": 42}],
    ],
    ids=["not-a-list", "not-a-mapping", "no-role", "role-is-not-a-string"],
)
def test_a_malformed_role_entry_is_dropped_rather_than_fatal(roles: object) -> None:
    principal = principal_from_claims(claims(roles=roles))

    # The token verified, so the platform issued it. Rejecting it over a field
    # added by a newer auth would break services whenever auth deploys first.
    assert principal.roles == ()


def test_a_salon_id_that_is_not_a_uuid_becomes_a_global_grant() -> None:
    principal = principal_from_claims(claims(roles=[{"role": "master", "salon_id": "nonsense"}]))

    assert principal.roles == (RoleClaim(role="master", salon_id=None),)


@pytest.mark.parametrize("missing", ["sub", "typ"])
def test_a_token_that_identifies_nobody_is_refused(missing: str) -> None:
    payload = claims()
    del payload[missing]

    with pytest.raises(ValueError, match="who it belongs to"):
        principal_from_claims(payload)


def test_holding_a_role_globally_satisfies_a_question_about_a_salon() -> None:
    principal = Principal(
        subject=str(USER_ID),
        token_type=ACCESS_TOKEN_TYPE,
        roles=(RoleClaim(role="master"),),
    )

    # A global master is a master in every salon.
    assert principal.holds("master", salon_id=SALON_ID) is True


def test_a_scoped_role_does_not_reach_another_salon() -> None:
    principal = Principal(
        subject=str(USER_ID),
        token_type=ACCESS_TOKEN_TYPE,
        roles=(RoleClaim(role="salon_admin", salon_id=SALON_ID),),
    )

    # The check that keeps one salon's administrator out of another's data.
    assert principal.holds("salon_admin", salon_id=SALON_ID) is True
    assert principal.holds("salon_admin", salon_id=UUID(int=1)) is False


def test_holding_any_of_several_roles_is_enough() -> None:
    principal = Principal(
        subject=str(USER_ID),
        token_type=ACCESS_TOKEN_TYPE,
        roles=(RoleClaim(role="salon_admin", salon_id=SALON_ID),),
    )

    assert principal.has_any_role(frozenset({"salon_admin", "super_admin"})) is True
    assert principal.has_any_role(frozenset({"super_admin"})) is False


def test_scopes_are_parsed_for_a_service_token() -> None:
    principal = principal_from_claims(
        claims(typ=SERVICE_TOKEN_TYPE, sub="booking", scopes=["catalog:read", 7])
    )

    assert principal.is_service is True
    assert principal.scopes == ("catalog:read",)
