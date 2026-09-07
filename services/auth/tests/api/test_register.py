"""POST /api/v1/auth/register through the real ASGI stack."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from barber_auth.models.user import User
from barber_auth.models.user_role import UserRole
from barber_common.errors import PROBLEM_CONTENT_TYPE
from barber_common.outbox.models import OutboxMessage
from barber_common.testing.fixtures import app_client

pytestmark = pytest.mark.integration

REGISTER = "/api/v1/auth/register"

EMAIL = "ivan@example.com"
PHONE = "+79991234567"
PASSWORD = "correct-horse-9"


def registration(**overrides: str) -> dict[str, str]:
    """A valid body. The test overrides the field its assertion is about."""
    return {"email": EMAIL, "phone": PHONE, "password": PASSWORD} | overrides


async def test_registering_answers_201_with_the_new_account(app: FastAPI) -> None:
    async with app_client(app) as client:
        response = await client.post(REGISTER, json=registration())

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == EMAIL
    assert body["phone"] == PHONE
    assert body["role"] == "client"
    assert body["email_confirmed"] is False


async def test_the_response_carries_nothing_secret(app: FastAPI) -> None:
    async with app_client(app) as client:
        response = await client.post(REGISTER, json=registration())

    body = response.json()
    assert "password" not in body
    assert "password_hash" not in body
    assert "token" not in body


async def test_the_new_account_holds_the_client_role(app: FastAPI, session: AsyncSession) -> None:
    async with app_client(app) as client:
        response = await client.post(REGISTER, json=registration())

    grants = (await session.execute(select(UserRole))).scalars().all()
    assert [grant.role for grant in grants] == ["client"]
    assert grants[0].salon_id is None
    assert str(grants[0].user_id) == response.json()["user_id"]


async def test_the_contacts_are_stored_normalised(app: FastAPI, session: AsyncSession) -> None:
    async with app_client(app) as client:
        await client.post(
            REGISTER, json=registration(email="Ivan@Example.COM", phone="8 999 123 45 67")
        )

    user = (await session.execute(select(User))).scalar_one()

    assert user.email == EMAIL
    assert user.phone == PHONE


async def test_a_duplicate_address_answers_409_with_its_domain_code(app: FastAPI) -> None:
    async with app_client(app) as client:
        await client.post(REGISTER, json=registration())

        response = await client.post(REGISTER, json=registration(phone="+79991234568"))

    assert response.status_code == 409
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    assert response.json()["code"] == "email_already_registered"


async def test_a_duplicate_address_in_another_case_is_still_a_duplicate(
    app: FastAPI,
) -> None:
    async with app_client(app) as client:
        await client.post(REGISTER, json=registration())

        response = await client.post(
            REGISTER, json=registration(email="IVAN@example.com", phone="+79991234568")
        )

    assert response.status_code == 409
    assert response.json()["code"] == "email_already_registered"


async def test_a_duplicate_phone_number_names_the_phone(app: FastAPI) -> None:
    async with app_client(app) as client:
        await client.post(REGISTER, json=registration())

        response = await client.post(REGISTER, json=registration(email="petr@example.com"))

    assert response.status_code == 409
    assert response.json()["code"] == "phone_already_registered"


async def test_a_rejected_registration_writes_nothing(app: FastAPI, session: AsyncSession) -> None:
    async with app_client(app) as client:
        await client.post(REGISTER, json=registration())
        await client.post(REGISTER, json=registration(email="petr@example.com"))

    assert len((await session.execute(select(User))).scalars().all()) == 1
    assert len((await session.execute(select(OutboxMessage))).scalars().all()) == 2


@pytest.mark.parametrize(
    "phone",
    ["99912345", "not a phone", "+0 999 123 45 67", "+7999123456789012"],
)
async def test_a_phone_number_that_is_not_one_answers_422(app: FastAPI, phone: str) -> None:
    async with app_client(app) as client:
        response = await client.post(REGISTER, json=registration(phone=phone))

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


@pytest.mark.parametrize("password", ["short1", "aaaaaaaaaaaa", "123456789012"])
async def test_a_weak_password_answers_422_without_quoting_it(app: FastAPI, password: str) -> None:
    async with app_client(app) as client:
        response = await client.post(REGISTER, json=registration(password=password))

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert password not in response.text


async def test_an_address_that_is_not_one_answers_422(app: FastAPI) -> None:
    async with app_client(app) as client:
        response = await client.post(REGISTER, json=registration(email="ivan"))

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_an_unknown_field_in_the_body_is_refused(app: FastAPI) -> None:
    async with app_client(app) as client:
        response = await client.post(REGISTER, json=registration() | {"role": "super_admin"})

    # Otherwise a caller could try to hand itself a role by guessing a field
    # name, and the failure would be silent.
    assert response.status_code == 422


async def test_the_registration_is_documented_in_the_openapi_contract(
    app: FastAPI,
) -> None:
    async with app_client(app) as client:
        document = (await client.get("/openapi.json")).json()

    operation = document["paths"]["/api/v1/auth/register"]["post"]
    assert set(operation["responses"]) >= {"201", "409", "422"}
