"""POST /api/v1/auth/confirm-email, and the token behind it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from barber_auth.adapters.argon2_hasher import Argon2Hasher
from barber_auth.domain.confirmation import hash_confirmation_token
from barber_auth.domain.identifiers import UserId
from barber_auth.models.email_confirmation import EmailConfirmation
from barber_auth.models.user import User
from barber_auth.services.email_confirmation import IssueEmailConfirmation
from barber_auth.services.registration import RegisterUser
from barber_common.db.session import transaction
from barber_common.outbox.models import OutboxMessage
from barber_common.testing.fixtures import app_client

pytestmark = pytest.mark.integration

CONFIRM = "/api/v1/auth/confirm-email"

EMAIL = "ivan@example.com"
PHONE = "+79991234567"
PASSWORD = "correct-horse-9"


@pytest.fixture
def register(session: AsyncSession, hasher: Argon2Hasher) -> RegisterUser:
    return RegisterUser(
        session=session,
        hasher=hasher,
        confirmation_ttl_hours=24,
        password_min_length=10,
    )


async def issued_token(session: AsyncSession, register: RegisterUser, **contacts: str) -> str:
    """Register a user and return the token from the letter it triggered.

    Read out of the event rather than out of the database: the database has
    only the hash, which is the property the rest of this file asserts.
    """
    await register.execute(
        email=contacts.get("email", EMAIL),
        phone=contacts.get("phone", PHONE),
        password=PASSWORD,
    )
    statement = select(OutboxMessage).where(
        OutboxMessage.event_type == "user.email_confirmation_requested"
    )
    event = (await session.execute(statement)).scalars().all()[-1]
    token = event.payload["token"]
    assert isinstance(token, str)
    return token


async def test_the_token_from_the_letter_confirms_the_address(
    app: FastAPI, session: AsyncSession, register: RegisterUser
) -> None:
    token = await issued_token(session, register)

    async with app_client(app) as client:
        response = await client.post(CONFIRM, json={"token": token})

    assert response.status_code == 200
    assert response.json()["email"] == EMAIL
    assert response.json()["email_confirmed"] is True


async def test_confirming_stamps_the_moment_on_the_user(
    app: FastAPI, session: AsyncSession, register: RegisterUser
) -> None:
    token = await issued_token(session, register)

    async with app_client(app) as client:
        await client.post(CONFIRM, json={"token": token})

    user = (await session.execute(select(User))).scalar_one()
    await session.refresh(user)
    assert user.email_confirmed_at is not None


async def test_confirming_publishes_the_event(
    app: FastAPI, session: AsyncSession, register: RegisterUser
) -> None:
    token = await issued_token(session, register)

    async with app_client(app) as client:
        await client.post(CONFIRM, json={"token": token})

    statement = select(OutboxMessage).where(OutboxMessage.event_type == "user.email_confirmed")
    event = (await session.execute(statement)).scalar_one()

    assert event.topic == "auth.users.v1"
    assert event.payload["email"] == EMAIL
    # The confirmed address is what notification needs; the token is not in it.
    assert "token" not in event.payload


async def test_the_token_is_spent_and_cannot_be_used_again(
    app: FastAPI, session: AsyncSession, register: RegisterUser
) -> None:
    token = await issued_token(session, register)

    async with app_client(app) as client:
        await client.post(CONFIRM, json={"token": token})

        response = await client.post(CONFIRM, json={"token": token})

    assert response.status_code == 422
    assert response.json()["code"] == "confirmation_token_invalid"


async def test_an_expired_token_is_refused(
    app: FastAPI, session: AsyncSession, register: RegisterUser
) -> None:
    # Issued a day and a half ago: the clock is not moved, the token is old.
    await register.execute(email=EMAIL, phone=PHONE, password=PASSWORD)
    user = (await session.execute(select(User))).scalar_one()
    async with transaction(session):
        issued = await IssueEmailConfirmation(session=session, ttl_hours=24).execute(
            user_id=UserId(user.id),
            email=user.email,
            now=datetime.now(UTC) - timedelta(hours=36),
        )

    async with app_client(app) as client:
        response = await client.post(CONFIRM, json={"token": issued.token})

    assert response.status_code == 422
    assert response.json()["code"] == "confirmation_token_invalid"


async def test_a_token_nobody_issued_is_refused(app: FastAPI) -> None:
    async with app_client(app) as client:
        response = await client.post(CONFIRM, json={"token": "a" * 43})

    assert response.status_code == 422
    assert response.json()["code"] == "confirmation_token_invalid"


async def test_the_token_of_another_user_confirms_only_that_user(
    app: FastAPI, session: AsyncSession, register: RegisterUser
) -> None:
    await issued_token(session, register)
    other_token = await issued_token(
        session, register, email="petr@example.com", phone="+79991234568"
    )

    async with app_client(app) as client:
        response = await client.post(CONFIRM, json={"token": other_token})

    assert response.json()["email"] == "petr@example.com"
    unconfirmed = (await session.execute(select(User).where(User.email == EMAIL))).scalar_one()
    await session.refresh(unconfirmed)
    assert unconfirmed.email_confirmed_at is None


async def test_an_unusable_token_answers_the_same_way_whatever_is_wrong_with_it(
    app: FastAPI, session: AsyncSession, register: RegisterUser
) -> None:
    token = await issued_token(session, register)

    async with app_client(app) as client:
        await client.post(CONFIRM, json={"token": token})
        spent = await client.post(CONFIRM, json={"token": token})
        unknown = await client.post(CONFIRM, json={"token": "b" * 43})

    # One answer for "spent" and "never existed": two would tell a caller which
    # tokens the service has issued.
    assert spent.status_code == unknown.status_code
    assert spent.json()["code"] == unknown.json()["code"]
    assert spent.json()["detail"] == unknown.json()["detail"]


async def test_only_the_hash_of_the_token_is_stored(
    session: AsyncSession, register: RegisterUser
) -> None:
    token = await issued_token(session, register)

    confirmation = (await session.execute(select(EmailConfirmation))).scalar_one()

    assert confirmation.token_hash == hash_confirmation_token(token)
    assert token not in confirmation.token_hash


async def test_the_confirmation_is_documented_in_the_openapi_contract(app: FastAPI) -> None:
    async with app_client(app) as client:
        document = (await client.get("/openapi.json")).json()

    operation = document["paths"]["/api/v1/auth/confirm-email"]["post"]
    assert set(operation["responses"]) >= {"200", "422"}
