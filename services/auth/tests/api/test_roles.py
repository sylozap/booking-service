"""POST and DELETE /api/v1/users/{id}/roles through the real ASGI stack.

Every request here carries a token that was really issued and is really
verified. A test that handed the endpoint a hand-built principal would stop
testing the thing these endpoints exist to do.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import uuid4

import jwt
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from barber_auth.domain.identifiers import SalonId
from barber_auth.domain.roles import Role
from barber_auth.models.user import User
from barber_auth.models.user_role import UserRole
from barber_common.testing.fixtures import app_client

pytestmark = pytest.mark.integration

LOGIN = "/api/v1/auth/login"
REFRESH = "/api/v1/auth/refresh"

SALON = SalonId(uuid4())
ANOTHER_SALON = SalonId(uuid4())

UserFactory = Callable[..., Awaitable[User]]
AuthorizationFactory = Callable[..., Awaitable[dict[str, str]]]


def roles_url(user: User) -> str:
    return f"/api/v1/users/{user.id}/roles"


async def grants_of(session: AsyncSession, user: User) -> list[UserRole]:
    statement = select(UserRole).where(UserRole.user_id == user.id)
    return list((await session.execute(statement)).scalars().all())


async def test_a_super_admin_grants_a_role(
    app_with_verifier: FastAPI,
    authorize: AuthorizationFactory,
    make_user: UserFactory,
    session: AsyncSession,
) -> None:
    headers = await authorize(roles=((Role.SUPER_ADMIN, None),))
    subject = await make_user(email="master@example.com", phone="+79990000001")

    async with app_client(app_with_verifier) as client:
        response = await client.post(
            roles_url(subject),
            json={"role": "salon_admin", "salon_id": str(SALON)},
            headers=headers,
        )

    assert response.status_code == 201
    assert response.json()["role"] == "salon_admin"
    assert {(grant.role, grant.salon_id) for grant in await grants_of(session, subject)} == {
        ("client", None),
        ("salon_admin", SALON),
    }


async def test_a_salon_admin_appoints_a_master_in_their_salon(
    app_with_verifier: FastAPI,
    authorize: AuthorizationFactory,
    make_user: UserFactory,
) -> None:
    headers = await authorize(roles=((Role.SALON_ADMIN, SALON),))
    subject = await make_user(email="master@example.com", phone="+79990000001")

    async with app_client(app_with_verifier) as client:
        response = await client.post(
            roles_url(subject),
            json={"role": "master", "salon_id": str(SALON)},
            headers=headers,
        )

    assert response.status_code == 201


async def test_a_salon_admin_cannot_appoint_a_super_admin(
    app_with_verifier: FastAPI,
    authorize: AuthorizationFactory,
    make_user: UserFactory,
) -> None:
    headers = await authorize(roles=((Role.SALON_ADMIN, SALON),))
    subject = await make_user(email="master@example.com", phone="+79990000001")

    async with app_client(app_with_verifier) as client:
        response = await client.post(
            roles_url(subject), json={"role": "super_admin"}, headers=headers
        )

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden_for_role"


async def test_a_salon_admin_cannot_reach_another_salon(
    app_with_verifier: FastAPI,
    authorize: AuthorizationFactory,
    make_user: UserFactory,
    session: AsyncSession,
) -> None:
    headers = await authorize(roles=((Role.SALON_ADMIN, SALON),))
    subject = await make_user(email="master@example.com", phone="+79990000001")

    async with app_client(app_with_verifier) as client:
        response = await client.post(
            roles_url(subject),
            json={"role": "master", "salon_id": str(ANOTHER_SALON)},
            headers=headers,
        )

    assert response.status_code == 403
    assert [grant.role for grant in await grants_of(session, subject)] == ["client"]


async def test_a_salon_admin_cannot_multiply_themselves(
    app_with_verifier: FastAPI,
    authorize: AuthorizationFactory,
    make_user: UserFactory,
) -> None:
    headers = await authorize(roles=((Role.SALON_ADMIN, SALON),))
    subject = await make_user(email="deputy@example.com", phone="+79990000001")

    async with app_client(app_with_verifier) as client:
        response = await client.post(
            roles_url(subject),
            json={"role": "salon_admin", "salon_id": str(SALON)},
            headers=headers,
        )

    # Handing over a salon is a decision for whoever granted it in the first
    # place; without this the owner is the last to know.
    assert response.status_code == 403


async def test_a_client_cannot_grant_anything(
    app_with_verifier: FastAPI,
    authorize: AuthorizationFactory,
    make_user: UserFactory,
) -> None:
    headers = await authorize()
    subject = await make_user(email="master@example.com", phone="+79990000001")

    async with app_client(app_with_verifier) as client:
        response = await client.post(
            roles_url(subject),
            json={"role": "master", "salon_id": str(SALON)},
            headers=headers,
        )

    assert response.status_code == 403


async def test_the_endpoint_is_closed_without_a_token(
    app_with_verifier: FastAPI, make_user: UserFactory
) -> None:
    subject = await make_user(email="master@example.com", phone="+79990000001")

    async with app_client(app_with_verifier) as client:
        response = await client.post(
            roles_url(subject), json={"role": "master", "salon_id": str(SALON)}
        )

    assert response.status_code == 401


async def test_the_client_role_cannot_be_granted(
    app_with_verifier: FastAPI,
    authorize: AuthorizationFactory,
    make_user: UserFactory,
) -> None:
    headers = await authorize(roles=((Role.SUPER_ADMIN, None),))
    subject = await make_user(email="someone@example.com", phone="+79990000001")

    async with app_client(app_with_verifier) as client:
        response = await client.post(roles_url(subject), json={"role": "client"}, headers=headers)

    # 422 and not 403: the caller here is a super_admin, so this is not a
    # permissions problem -- the role simply is not handed out this way.
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_a_salon_scoped_role_without_a_salon_is_refused(
    app_with_verifier: FastAPI,
    authorize: AuthorizationFactory,
    make_user: UserFactory,
) -> None:
    headers = await authorize(roles=((Role.SUPER_ADMIN, None),))
    subject = await make_user(email="someone@example.com", phone="+79990000001")

    async with app_client(app_with_verifier) as client:
        response = await client.post(
            roles_url(subject), json={"role": "salon_admin"}, headers=headers
        )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_granting_a_role_twice_is_not_a_conflict(
    app_with_verifier: FastAPI,
    authorize: AuthorizationFactory,
    make_user: UserFactory,
    session: AsyncSession,
) -> None:
    headers = await authorize(roles=((Role.SUPER_ADMIN, None),))
    subject = await make_user(email="master@example.com", phone="+79990000001")
    body = {"role": "master", "salon_id": str(SALON)}

    async with app_client(app_with_verifier) as client:
        await client.post(roles_url(subject), json=body, headers=headers)
        response = await client.post(roles_url(subject), json=body, headers=headers)

    # The requested state is the state that exists. A retried request must not
    # look like a failure.
    assert response.status_code == 201
    assert len(await grants_of(session, subject)) == 2


async def test_granting_to_an_unknown_user_answers_404(
    app_with_verifier: FastAPI, authorize: AuthorizationFactory
) -> None:
    headers = await authorize(roles=((Role.SUPER_ADMIN, None),))

    async with app_client(app_with_verifier) as client:
        response = await client.post(
            f"/api/v1/users/{uuid4()}/roles",
            json={"role": "master", "salon_id": str(SALON)},
            headers=headers,
        )

    assert response.status_code == 404


async def test_a_granted_role_reaches_the_next_access_token(
    app_with_verifier: FastAPI,
    authorize: AuthorizationFactory,
    make_user: UserFactory,
    signer: object,
) -> None:
    admin = await authorize(roles=((Role.SUPER_ADMIN, None),))
    subject = await make_user(email="master@example.com", phone="+79990000001")

    async with app_client(app_with_verifier) as client:
        before = (
            await client.post(LOGIN, json={"email": "master@example.com", "password": PASSWORD})
        ).json()
        await client.post(
            roles_url(subject),
            json={"role": "master", "salon_id": str(SALON)},
            headers=admin,
        )
        after = (await client.post(REFRESH, json={"refresh_token": before["refresh_token"]})).json()

    # The token issued before the grant says nothing about it; the one issued
    # after the refresh does. That fifteen-minute delay is the cost of
    # verifying tokens without asking anyone (ADR-0010).
    assert _roles_in(before["access_token"]) == [{"role": "client", "salon_id": None}]
    assert {"role": "master", "salon_id": str(SALON)} in _roles_in(after["access_token"])


async def test_revoking_removes_the_grant(
    app_with_verifier: FastAPI,
    authorize: AuthorizationFactory,
    make_user: UserFactory,
    session: AsyncSession,
) -> None:
    headers = await authorize(roles=((Role.SUPER_ADMIN, None),))
    subject = await make_user(
        email="master@example.com",
        phone="+79990000001",
        roles=((Role.CLIENT, None), (Role.MASTER, SALON)),
    )

    async with app_client(app_with_verifier) as client:
        response = await client.request(
            "DELETE",
            roles_url(subject),
            json={"role": "master", "salon_id": str(SALON)},
            headers=headers,
        )

    assert response.status_code == 204
    assert [grant.role for grant in await grants_of(session, subject)] == ["client"]


async def test_revoking_a_global_grant_does_not_touch_the_scoped_one(
    app_with_verifier: FastAPI,
    authorize: AuthorizationFactory,
    make_user: UserFactory,
    session: AsyncSession,
) -> None:
    headers = await authorize(roles=((Role.SUPER_ADMIN, None),))
    subject = await make_user(
        email="master@example.com",
        phone="+79990000001",
        roles=((Role.MASTER, None), (Role.MASTER, SALON)),
    )

    async with app_client(app_with_verifier) as client:
        await client.request("DELETE", roles_url(subject), json={"role": "master"}, headers=headers)

    # A global grant and a scoped one are different rows of the same name, and
    # "salon_id IS NULL" is what tells them apart -- "= NULL" would match
    # neither and silently revoke nothing.
    remaining = await grants_of(session, subject)
    assert [(grant.role, grant.salon_id) for grant in remaining] == [("master", SALON)]


async def test_revoking_a_role_that_is_not_held_answers_404(
    app_with_verifier: FastAPI,
    authorize: AuthorizationFactory,
    make_user: UserFactory,
) -> None:
    headers = await authorize(roles=((Role.SUPER_ADMIN, None),))
    subject = await make_user(email="someone@example.com", phone="+79990000001")

    async with app_client(app_with_verifier) as client:
        response = await client.request(
            "DELETE",
            roles_url(subject),
            json={"role": "master", "salon_id": str(SALON)},
            headers=headers,
        )

    assert response.status_code == 404


async def test_a_salon_admin_cannot_revoke_outside_their_salon(
    app_with_verifier: FastAPI,
    authorize: AuthorizationFactory,
    make_user: UserFactory,
    session: AsyncSession,
) -> None:
    headers = await authorize(roles=((Role.SALON_ADMIN, SALON),))
    subject = await make_user(
        email="master@example.com",
        phone="+79990000001",
        roles=((Role.MASTER, ANOTHER_SALON),),
    )

    async with app_client(app_with_verifier) as client:
        response = await client.request(
            "DELETE",
            roles_url(subject),
            json={"role": "master", "salon_id": str(ANOTHER_SALON)},
            headers=headers,
        )

    assert response.status_code == 403
    assert len(await grants_of(session, subject)) == 1


async def test_an_unknown_field_in_the_body_is_refused(
    app_with_verifier: FastAPI,
    authorize: AuthorizationFactory,
    make_user: UserFactory,
) -> None:
    headers = await authorize(roles=((Role.SUPER_ADMIN, None),))
    subject = await make_user(email="someone@example.com", phone="+79990000001")

    async with app_client(app_with_verifier) as client:
        response = await client.post(
            roles_url(subject),
            json={"role": "master", "salon_id": str(SALON), "granted_by": "me"},
            headers=headers,
        )

    assert response.status_code == 422


async def test_the_endpoints_are_documented_in_the_openapi_contract(
    app_with_verifier: FastAPI,
) -> None:
    async with app_client(app_with_verifier) as client:
        document = (await client.get("/openapi.json")).json()

    operations = document["paths"]["/api/v1/users/{user_id}/roles"]
    assert set(operations["post"]["responses"]) >= {"201", "403", "404", "422"}
    # The delay before a granted role appears in a token is part of the
    # contract, not folklore (ADR-0010).
    assert "refresh" in operations["post"]["description"].lower()


PASSWORD = "correct-horse-9"


def _roles_in(access_token: str) -> list[dict[str, str | None]]:
    """The roles claim of a token, read without verifying it.

    Acceptable here and nowhere else: the token was just issued by the service
    under test, and the assertion is about what it says, not whether it is
    trustworthy.
    """
    claims = jwt.decode(access_token, options={"verify_signature": False})
    roles = claims["roles"]
    assert isinstance(roles, list)
    return roles
