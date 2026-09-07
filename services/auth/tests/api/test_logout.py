"""POST /api/v1/auth/logout: ending one session, or all of them."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from barber_auth.models.refresh_token import RefreshToken
from barber_auth.models.user import User
from barber_common.testing.fixtures import app_client

pytestmark = pytest.mark.integration

LOGIN = "/api/v1/auth/login"
LOGOUT = "/api/v1/auth/logout"
REFRESH = "/api/v1/auth/refresh"

EMAIL = "ivan@example.com"
PASSWORD = "correct-horse-9"

UserFactory = Callable[..., Awaitable[User]]


async def two_sessions(app: FastAPI, make_user: UserFactory) -> tuple[str, str]:
    """One account signed in twice, as a phone and a laptop would be."""
    await make_user(email=EMAIL, password=PASSWORD)
    async with app_client(app) as client:
        body = {"email": EMAIL, "password": PASSWORD}
        first = (await client.post(LOGIN, json=body)).json()["refresh_token"]
        second = (await client.post(LOGIN, json=body)).json()["refresh_token"]
    assert isinstance(first, str) and isinstance(second, str)
    return first, second


async def test_logging_out_answers_204(app: FastAPI, make_user: UserFactory) -> None:
    phone, _ = await two_sessions(app, make_user)

    async with app_client(app) as client:
        response = await client.post(LOGOUT, json={"refresh_token": phone})

    assert response.status_code == 204
    assert response.content == b""


async def test_the_session_that_logged_out_cannot_be_renewed(
    app: FastAPI, make_user: UserFactory
) -> None:
    phone, _ = await two_sessions(app, make_user)

    async with app_client(app) as client:
        await client.post(LOGOUT, json={"refresh_token": phone})
        response = await client.post(REFRESH, json={"refresh_token": phone})

    assert response.status_code == 401


async def test_the_other_sessions_are_untouched(app: FastAPI, make_user: UserFactory) -> None:
    phone, laptop = await two_sessions(app, make_user)

    async with app_client(app) as client:
        await client.post(LOGOUT, json={"refresh_token": phone})
        response = await client.post(REFRESH, json={"refresh_token": laptop})

    # Signing out of a phone must not sign the same person out of their laptop.
    assert response.status_code == 200


async def test_logging_out_everywhere_ends_every_session(
    app: FastAPI, make_user: UserFactory
) -> None:
    phone, laptop = await two_sessions(app, make_user)

    async with app_client(app) as client:
        await client.post(LOGOUT, json={"refresh_token": phone, "all_devices": True})
        from_phone = await client.post(REFRESH, json={"refresh_token": phone})
        from_laptop = await client.post(REFRESH, json={"refresh_token": laptop})

    assert from_phone.status_code == 401
    assert from_laptop.status_code == 401


async def test_logging_out_everywhere_revokes_the_rows_rather_than_deleting_them(
    app: FastAPI, make_user: UserFactory, session: AsyncSession
) -> None:
    phone, _ = await two_sessions(app, make_user)

    async with app_client(app) as client:
        await client.post(LOGOUT, json={"refresh_token": phone, "all_devices": True})

    tokens = (await session.execute(select(RefreshToken))).scalars().all()
    # Kept, so a later presentation is detectable reuse rather than an unknown
    # token, and so the cleanup of T1.12 has something to prune.
    assert len(tokens) == 2
    assert all(token.revoked_at is not None for token in tokens)


async def test_logging_out_does_not_touch_another_user(
    app: FastAPI, make_user: UserFactory
) -> None:
    phone, _ = await two_sessions(app, make_user)
    await make_user(email="petr@example.com", phone="+79991234568", password=PASSWORD)
    async with app_client(app) as client:
        stranger = (
            await client.post(LOGIN, json={"email": "petr@example.com", "password": PASSWORD})
        ).json()["refresh_token"]

        await client.post(LOGOUT, json={"refresh_token": phone, "all_devices": True})
        response = await client.post(REFRESH, json={"refresh_token": stranger})

    assert response.status_code == 200


async def test_an_unknown_token_answers_204(app: FastAPI) -> None:
    async with app_client(app) as client:
        response = await client.post(LOGOUT, json={"refresh_token": "x" * 43})

    # Logging out is meant to be safe to repeat, and answering differently for
    # a token that exists would be a way to test whether one does.
    assert response.status_code == 204


async def test_logging_out_twice_is_not_treated_as_theft(
    app: FastAPI, make_user: UserFactory, session: AsyncSession
) -> None:
    phone, laptop = await two_sessions(app, make_user)

    async with app_client(app) as client:
        await client.post(LOGOUT, json={"refresh_token": phone})
        response = await client.post(LOGOUT, json={"refresh_token": phone})

        # A retry after a timeout looks exactly like this. Only the rotation
        # endpoint reads a spent token as evidence of a stolen one.
        still_live = await client.post(REFRESH, json={"refresh_token": laptop})

    assert response.status_code == 204
    assert still_live.status_code == 200
    assert (await session.execute(select(RefreshToken))).scalars().all() != []


async def test_an_unknown_field_in_the_body_is_refused(app: FastAPI) -> None:
    async with app_client(app) as client:
        response = await client.post(
            LOGOUT, json={"refresh_token": "x" * 43, "user_id": "someone-else"}
        )

    # Otherwise a caller could try to end someone else's sessions by guessing
    # a field name, and the failure would be silent.
    assert response.status_code == 422


async def test_the_logout_is_documented_in_the_openapi_contract(app: FastAPI) -> None:
    async with app_client(app) as client:
        document = (await client.get("/openapi.json")).json()

    operation = document["paths"]["/api/v1/auth/logout"]["post"]
    assert "204" in operation["responses"]
    # The access token outliving the logout is a property of the design, not an
    # oversight, and it is written where a client integrating against the API
    # will read it (ADR-0010).
    assert "expire" in operation["description"].lower()
