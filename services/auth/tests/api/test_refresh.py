"""POST /api/v1/auth/refresh: rotation, and the theft it is meant to catch.

The tests here are the security-relevant ones of the service. A rotation that
quietly hands out two valid pairs for one token is indistinguishable from a
working implementation until someone's session is stolen.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from barber_auth.domain.tokens import hash_refresh_token
from barber_auth.models.refresh_token import RefreshToken
from barber_auth.models.user import User
from barber_common.testing.fixtures import app_client

pytestmark = pytest.mark.integration

LOGIN = "/api/v1/auth/login"
REFRESH = "/api/v1/auth/refresh"

EMAIL = "ivan@example.com"
PASSWORD = "correct-horse-9"

UserFactory = Callable[..., Awaitable[User]]


async def signed_in(app: FastAPI, make_user: UserFactory, **overrides: object) -> str:
    """Create an account, log in, and return the refresh token of the session."""
    await make_user(email=EMAIL, password=PASSWORD, **overrides)
    async with app_client(app) as client:
        response = await client.post(LOGIN, json={"email": EMAIL, "password": PASSWORD})
    token = response.json()["refresh_token"]
    assert isinstance(token, str)
    return token


async def test_a_live_token_is_exchanged_for_a_new_pair(
    app: FastAPI, make_user: UserFactory
) -> None:
    refresh_token = await signed_in(app, make_user)

    async with app_client(app) as client:
        response = await client.post(REFRESH, json={"refresh_token": refresh_token})

    assert response.status_code == 200
    body = response.json()
    assert body["refresh_token"] != refresh_token
    assert body["access_token"]


async def test_the_new_token_stays_in_the_same_family(
    app: FastAPI, make_user: UserFactory, session: AsyncSession
) -> None:
    refresh_token = await signed_in(app, make_user)

    async with app_client(app) as client:
        await client.post(REFRESH, json={"refresh_token": refresh_token})

    families = {
        token.family_id for token in (await session.execute(select(RefreshToken))).scalars()
    }
    # One family is one session. A rotation continues it; only a login starts
    # a new one.
    assert len(families) == 1


async def test_the_old_token_is_retired_and_points_at_its_successor(
    app: FastAPI, make_user: UserFactory, session: AsyncSession
) -> None:
    refresh_token = await signed_in(app, make_user)

    async with app_client(app) as client:
        response = await client.post(REFRESH, json={"refresh_token": refresh_token})

    statement = select(RefreshToken).where(
        RefreshToken.token_hash == hash_refresh_token(refresh_token)
    )
    old = (await session.execute(statement)).scalar_one()
    successor = (
        await session.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == hash_refresh_token(response.json()["refresh_token"])
            )
        )
    ).scalar_one()

    assert old.revoked_at is not None
    # What lets an incident be read forward through a family.
    assert old.replaced_by == successor.id


async def test_the_old_token_stops_working(app: FastAPI, make_user: UserFactory) -> None:
    refresh_token = await signed_in(app, make_user)

    async with app_client(app) as client:
        await client.post(REFRESH, json={"refresh_token": refresh_token})
        response = await client.post(REFRESH, json={"refresh_token": refresh_token})

    assert response.status_code == 401


async def test_reusing_a_spent_token_revokes_the_whole_family(
    app: FastAPI, make_user: UserFactory, session: AsyncSession
) -> None:
    refresh_token = await signed_in(app, make_user)

    async with app_client(app) as client:
        rotated = await client.post(REFRESH, json={"refresh_token": refresh_token})
        await client.post(REFRESH, json={"refresh_token": refresh_token})

        # The pair handed to the honest client a moment ago dies with the rest:
        # there is no way to tell which of the two holders is calling.
        response = await client.post(
            REFRESH, json={"refresh_token": rotated.json()["refresh_token"]}
        )

    assert response.status_code == 401
    live = [
        token
        for token in (await session.execute(select(RefreshToken))).scalars()
        if token.revoked_at is None
    ]
    assert live == []


async def test_reuse_answers_exactly_like_an_expired_token(
    app: FastAPI, make_user: UserFactory, session: AsyncSession
) -> None:
    refresh_token = await signed_in(app, make_user)
    async with app_client(app) as client:
        await client.post(REFRESH, json={"refresh_token": refresh_token})
        reused = await client.post(REFRESH, json={"refresh_token": refresh_token})

    expired = await _expired_token(session, make_user, email="petr@example.com")
    async with app_client(app) as client:
        stale = await client.post(REFRESH, json={"refresh_token": expired})

    # Naming the reuse would tell whoever stole the token that it was noticed.
    assert reused.status_code == stale.status_code == 401
    assert reused.json()["code"] == stale.json()["code"]
    assert reused.json()["detail"] == stale.json()["detail"]


async def test_an_expired_token_is_refused(
    app: FastAPI, make_user: UserFactory, session: AsyncSession
) -> None:
    expired = await _expired_token(session, make_user)

    async with app_client(app) as client:
        response = await client.post(REFRESH, json={"refresh_token": expired})

    assert response.status_code == 401


async def test_an_expiry_does_not_kill_the_other_sessions(
    app: FastAPI, make_user: UserFactory, session: AsyncSession
) -> None:
    await make_user(email=EMAIL, password=PASSWORD)
    async with app_client(app) as client:
        live = (await client.post(LOGIN, json={"email": EMAIL, "password": PASSWORD})).json()[
            "refresh_token"
        ]

    expired = await _expire_a_new_session(app, session)

    async with app_client(app) as client:
        await client.post(REFRESH, json={"refresh_token": expired})
        response = await client.post(REFRESH, json={"refresh_token": live})

    # Coming back after a holiday is not theft, and must not log the user out
    # of the devices they are still using.
    assert response.status_code == 200


async def test_an_unknown_token_is_refused(app: FastAPI) -> None:
    async with app_client(app) as client:
        response = await client.post(REFRESH, json={"refresh_token": "x" * 43})

    assert response.status_code == 401


async def test_a_token_of_a_deactivated_account_stops_working(
    app: FastAPI, make_user: UserFactory, session: AsyncSession
) -> None:
    refresh_token = await signed_in(app, make_user)
    user = (await session.execute(select(User))).scalar_one()
    user.is_active = False
    await session.commit()

    async with app_client(app) as client:
        response = await client.post(REFRESH, json={"refresh_token": refresh_token})

    assert response.status_code == 401


async def test_an_unknown_field_in_the_body_is_refused(app: FastAPI) -> None:
    async with app_client(app) as client:
        response = await client.post(
            REFRESH, json={"refresh_token": "x" * 43, "user_id": "someone-else"}
        )

    assert response.status_code == 422


async def test_the_rotation_is_documented_in_the_openapi_contract(app: FastAPI) -> None:
    async with app_client(app) as client:
        document = (await client.get("/openapi.json")).json()

    operation = document["paths"]["/api/v1/auth/refresh"]["post"]
    assert set(operation["responses"]) >= {"200", "401"}


async def _expired_token(session: AsyncSession, make_user: UserFactory, email: str = EMAIL) -> str:
    """A refresh token whose row is already past its expiry.

    Built by moving the row back in time rather than by waiting.
    """
    user = await make_user(email=email, phone=f"+7999123{hash(email) % 10000:04d}")
    token = "expired-" + "y" * 40
    session.add(
        RefreshToken(
            user_id=user.id,
            family_id=user.id,
            token_hash=hash_refresh_token(token),
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    )
    await session.commit()
    return token


async def _expire_a_new_session(app: FastAPI, session: AsyncSession) -> str:
    """Log in a second time and push that session's token past its expiry."""
    async with app_client(app) as client:
        token = (await client.post(LOGIN, json={"email": EMAIL, "password": PASSWORD})).json()[
            "refresh_token"
        ]

    statement = select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(token))
    row = (await session.execute(statement)).scalar_one()
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()
    return str(token)
