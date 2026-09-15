"""POST /api/v1/auth/login through the real ASGI stack."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from barber_auth.adapters.rsa_signer import RsaTokenSigner, public_jwk_of_pem
from barber_auth.domain.identifiers import SalonId
from barber_auth.domain.roles import Role
from barber_auth.domain.tokens import hash_refresh_token
from barber_auth.models.refresh_token import RefreshToken
from barber_auth.models.user import User
from barber_common.errors import PROBLEM_CONTENT_TYPE
from barber_common.testing.fixtures import app_client

pytestmark = pytest.mark.integration

# The ``make_user`` fixture of conftest. Spelled out here rather than imported:
# pytest runs this suite in importlib mode, so the conftest is not a module a
# test file can import from.
UserFactory = Callable[..., Awaitable[User]]

LOGIN = "/api/v1/auth/login"

EMAIL = "ivan@example.com"
PASSWORD = "correct-horse-9"


def credentials(**overrides: str) -> dict[str, str]:
    """A valid body. The test overrides the field its assertion is about."""
    return {"email": EMAIL, "password": PASSWORD} | overrides


def claims_of(token: str, signer: RsaTokenSigner) -> dict[str, object]:
    """Verify an access token the way a consuming service would.

    Through the published JWK and with the signature checked: reading the
    payload without verifying would let a test pass on a token no service
    would accept.
    """
    published = public_jwk_of_pem(signer.public_pem, kid=signer.kid)
    decoded = jwt.decode(
        token,
        jwt.PyJWK.from_dict(published).key,
        algorithms=["RS256"],
        options={"verify_aud": False},
    )
    assert isinstance(decoded, dict)
    return decoded


async def test_correct_credentials_return_a_token_pair(
    app: FastAPI, make_user: UserFactory
) -> None:
    await make_user(email=EMAIL, password=PASSWORD)

    async with app_client(app) as client:
        response = await client.post(LOGIN, json=credentials())

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "Bearer"
    assert body["access_token"]
    assert body["refresh_token"]


async def test_the_access_token_verifies_against_the_signing_key(
    app: FastAPI, make_user: UserFactory, signer: RsaTokenSigner
) -> None:
    user = await make_user(email=EMAIL, password=PASSWORD)

    async with app_client(app) as client:
        response = await client.post(LOGIN, json=credentials())

    assert claims_of(response.json()["access_token"], signer)["sub"] == str(user.id)


async def test_the_access_token_expires_after_fifteen_minutes(
    app: FastAPI, make_user: UserFactory, signer: RsaTokenSigner
) -> None:
    await make_user(email=EMAIL, password=PASSWORD)

    async with app_client(app) as client:
        response = await client.post(LOGIN, json=credentials())

    claims = claims_of(response.json()["access_token"], signer)
    lifetime = int(claims["exp"]) - int(claims["iat"])  # type: ignore[call-overload]  # NumericDate
    assert lifetime == 15 * 60


async def test_the_access_token_carries_the_roles_with_their_salon(
    app: FastAPI, make_user: UserFactory, signer: RsaTokenSigner
) -> None:
    salon_id = SalonId(uuid4())
    await make_user(
        email=EMAIL,
        password=PASSWORD,
        roles=((Role.CLIENT, None), (Role.SALON_ADMIN, salon_id)),
    )

    async with app_client(app) as client:
        response = await client.post(LOGIN, json=credentials())

    # A service reading this token can tell which salon the admin role applies
    # to without asking auth.
    assert claims_of(response.json()["access_token"], signer)["roles"] == [
        {"role": "client", "salon_id": None},
        {"role": "salon_admin", "salon_id": str(salon_id)},
    ]


async def test_the_refresh_token_is_stored_only_as_a_hash(
    app: FastAPI, make_user: UserFactory, session: AsyncSession
) -> None:
    await make_user(email=EMAIL, password=PASSWORD)

    async with app_client(app) as client:
        response = await client.post(LOGIN, json=credentials())

    issued = response.json()["refresh_token"]
    stored = (await session.execute(select(RefreshToken))).scalar_one()
    # A dump of this table must not be a set of live sessions.
    assert stored.token_hash != issued
    assert stored.token_hash == hash_refresh_token(issued)


async def test_the_refresh_token_lives_for_thirty_days(
    app: FastAPI, make_user: UserFactory, session: AsyncSession
) -> None:
    await make_user(email=EMAIL, password=PASSWORD)

    async with app_client(app) as client:
        response = await client.post(LOGIN, json=credentials())

    expires_at = datetime.fromisoformat(response.json()["refresh_expires_at"])
    # Against the nominal thirty days with a minute of slack, not against
    # ``.days``: a lifetime one second short of thirty days truncates to 29 and
    # would make this test fail on nothing.
    assert abs(expires_at - datetime.now(UTC) - timedelta(days=30)) < timedelta(minutes=1)
    assert (await session.execute(select(RefreshToken))).scalar_one().expires_at is not None


async def test_each_login_opens_a_family_of_its_own(
    app: FastAPI, make_user: UserFactory, session: AsyncSession
) -> None:
    await make_user(email=EMAIL, password=PASSWORD)

    async with app_client(app) as client:
        await client.post(LOGIN, json=credentials())
        await client.post(LOGIN, json=credentials())

    tokens = (await session.execute(select(RefreshToken))).scalars()
    families = {token.family_id for token in tokens}
    # Otherwise reuse detection on a phone would log the same user out of their
    # laptop, and logging out of one device would end both sessions.
    assert len(families) == 2


async def test_a_wrong_password_answers_401(app: FastAPI, make_user: UserFactory) -> None:
    await make_user(email=EMAIL, password=PASSWORD)

    async with app_client(app) as client:
        response = await client.post(LOGIN, json=credentials(password="wrong-horse-9"))

    assert response.status_code == 401
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    assert response.json()["code"] == "unauthorized"


async def test_an_unknown_address_answers_exactly_like_a_wrong_password(
    app: FastAPI, make_user: UserFactory
) -> None:
    await make_user(email=EMAIL, password=PASSWORD)

    async with app_client(app) as client:
        unknown = await client.post(LOGIN, json=credentials(email="nobody@example.com"))
        wrong = await client.post(LOGIN, json=credentials(password="wrong-horse-9"))

    # Any difference here is a way to enumerate the accounts of the platform.
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["code"] == wrong.json()["code"]
    assert unknown.json()["detail"] == wrong.json()["detail"]


async def test_a_deactivated_account_answers_like_an_unknown_one(
    app: FastAPI, make_user: UserFactory
) -> None:
    await make_user(email=EMAIL, password=PASSWORD, is_active=False)

    async with app_client(app) as client:
        response = await client.post(LOGIN, json=credentials())
        unknown = await client.post(LOGIN, json=credentials(email="nobody@example.com"))

    assert response.status_code == 401
    assert response.json()["code"] == unknown.json()["code"]


async def test_an_unconfirmed_address_answers_403_with_its_domain_code(
    app: FastAPI, make_user: UserFactory
) -> None:
    await make_user(email=EMAIL, password=PASSWORD, confirmed=False)

    async with app_client(app) as client:
        response = await client.post(LOGIN, json=credentials())

    # The exception to the rule above, and deliberately so: the caller has
    # already proven they know the password, and a user who never clicked the
    # link otherwise has no way to learn why they cannot get in.
    assert response.status_code == 403
    assert response.json()["code"] == "email_not_confirmed"


async def test_an_unconfirmed_account_gets_no_tokens(
    app: FastAPI, make_user: UserFactory, session: AsyncSession
) -> None:
    await make_user(email=EMAIL, password=PASSWORD, confirmed=False)

    async with app_client(app) as client:
        await client.post(LOGIN, json=credentials())

    assert (await session.execute(select(RefreshToken))).scalars().all() == []


async def test_a_failed_login_writes_no_session(
    app: FastAPI, make_user: UserFactory, session: AsyncSession
) -> None:
    await make_user(email=EMAIL, password=PASSWORD)

    async with app_client(app) as client:
        await client.post(LOGIN, json=credentials(password="wrong-horse-9"))

    assert (await session.execute(select(RefreshToken))).scalars().all() == []


async def test_the_response_never_quotes_the_password(app: FastAPI, make_user: UserFactory) -> None:
    await make_user(email=EMAIL, password=PASSWORD)

    async with app_client(app) as client:
        response = await client.post(LOGIN, json=credentials(password="wrong-horse-9"))

    assert "wrong-horse-9" not in response.text


async def test_an_address_that_is_not_one_answers_422(app: FastAPI) -> None:
    async with app_client(app) as client:
        response = await client.post(LOGIN, json=credentials(email="ivan"))

    assert response.status_code == 422


async def test_an_unknown_field_in_the_body_is_refused(app: FastAPI) -> None:
    async with app_client(app) as client:
        response = await client.post(LOGIN, json=credentials() | {"roles": ["super_admin"]})

    assert response.status_code == 422


async def test_the_login_is_documented_in_the_openapi_contract(app: FastAPI) -> None:
    async with app_client(app) as client:
        document = (await client.get("/openapi.json")).json()

    operation = document["paths"]["/api/v1/auth/login"]["post"]
    assert set(operation["responses"]) >= {"200", "401", "403"}
