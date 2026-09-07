"""Closing an endpoint with one dependency, through the real ASGI stack."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import APIRouter, Depends, FastAPI

from barber_common.auth import (
    ACCESS_TOKEN_TYPE,
    SERVICE_TOKEN_TYPE,
    Principal,
    StaticKeys,
    TokenVerifier,
    current_user,
    require_roles,
    require_service_token,
    use_authentication,
)
from barber_common.config import BaseAppSettings, Environment
from barber_common.errors import PROBLEM_CONTENT_TYPE
from barber_common.testing.fixtures import app_client

ISSUER = "https://barber.local/auth"
KID = "test-key"
SALON_ID = uuid4()


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def app(signing_key: rsa.RSAPrivateKey) -> FastAPI:
    """A service with three endpoints, closed three different ways."""
    from barber_common.app import create_app

    router = APIRouter()

    @router.get("/me")
    async def me(caller: Annotated[Principal, Depends(current_user)]) -> dict[str, str]:
        return {"subject": caller.subject}

    @router.get("/admin")
    async def admin(
        caller: Annotated[Principal, Depends(require_roles("salon_admin", "super_admin"))],
    ) -> dict[str, str]:
        return {"subject": caller.subject}

    @router.get("/internal/v1/things")
    async def internal(
        caller: Annotated[Principal, Depends(require_service_token("catalog:read"))],
    ) -> dict[str, str]:
        return {"subject": caller.subject}

    application = create_app(
        BaseAppSettings(
            service_name="catalog",
            environment=Environment.TEST,
            database_dsn="postgresql+asyncpg://catalog:secret@localhost:5432/catalog",
            redis_dsn="redis://localhost:6379/0",
            kafka_bootstrap_servers="localhost:9092",
        ),
        routers=[router],
    )
    use_authentication(
        application,
        TokenVerifier(
            keys=StaticKeys.from_pem(kid=KID, public_pem=_public_pem(signing_key)),
            issuer=ISSUER,
        ),
    )
    return application


def _public_pem(key: rsa.RSAPrivateKey) -> str:
    return (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )


def bearer(key: rsa.RSAPrivateKey, **overrides: object) -> dict[str, str]:
    """An Authorization header carrying a token of this platform."""
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "sub": str(uuid4()),
        "typ": ACCESS_TOKEN_TYPE,
        "roles": [{"role": "client", "salon_id": None}],
        "iss": ISSUER,
        "jti": str(uuid4()),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=15)).timestamp()),
    }
    claims.update(overrides)
    token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": KID})
    return {"Authorization": f"Bearer {token}"}


async def test_a_valid_token_is_let_through(app: FastAPI, signing_key: rsa.RSAPrivateKey) -> None:
    subject = str(uuid4())

    async with app_client(app) as client:
        response = await client.get("/me", headers=bearer(signing_key, sub=subject))

    assert response.status_code == 200
    assert response.json() == {"subject": subject}


async def test_a_request_without_a_token_answers_401(app: FastAPI) -> None:
    async with app_client(app) as client:
        response = await client.get("/me")

    assert response.status_code == 401
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    assert response.json()["code"] == "unauthorized"


@pytest.mark.parametrize(
    "header",
    ["", "Basic abc", "Bearer", "Bearer    ", "token-without-a-scheme"],
    ids=["empty", "wrong-scheme", "no-token", "blank-token", "no-scheme"],
)
async def test_a_malformed_authorization_header_answers_401(app: FastAPI, header: str) -> None:
    async with app_client(app) as client:
        response = await client.get("/me", headers={"Authorization": header})

    assert response.status_code == 401


async def test_an_expired_token_answers_401(app: FastAPI, signing_key: rsa.RSAPrivateKey) -> None:
    past = datetime.now(UTC) - timedelta(hours=1)

    async with app_client(app) as client:
        response = await client.get(
            "/me",
            headers=bearer(
                signing_key,
                iat=int(past.timestamp()),
                exp=int((past + timedelta(minutes=15)).timestamp()),
            ),
        )

    assert response.status_code == 401


async def test_a_forged_signature_answers_401(app: FastAPI) -> None:
    stranger = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    async with app_client(app) as client:
        response = await client.get("/me", headers=bearer(stranger))

    assert response.status_code == 401


async def test_the_gateway_headers_are_not_trusted(app: FastAPI) -> None:
    async with app_client(app) as client:
        response = await client.get(
            "/me", headers={"X-User-Id": str(uuid4()), "X-Roles": "super_admin"}
        )

    # A request reaching a pod past the gateway -- a port-forward, a neighbour
    # in the cluster, a hole in a NetworkPolicy -- is rejected by the pod
    # itself (ADR-0010).
    assert response.status_code == 401


async def test_a_sufficient_role_is_let_through(
    app: FastAPI, signing_key: rsa.RSAPrivateKey
) -> None:
    async with app_client(app) as client:
        response = await client.get(
            "/admin",
            headers=bearer(signing_key, roles=[{"role": "salon_admin", "salon_id": str(SALON_ID)}]),
        )

    assert response.status_code == 200


async def test_an_insufficient_role_answers_403(
    app: FastAPI, signing_key: rsa.RSAPrivateKey
) -> None:
    async with app_client(app) as client:
        response = await client.get("/admin", headers=bearer(signing_key))

    # 403 and not 401: the caller is who they say they are, and retrying with
    # the same credentials will not help.
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden_for_role"


async def test_any_one_of_the_named_roles_is_enough(
    app: FastAPI, signing_key: rsa.RSAPrivateKey
) -> None:
    async with app_client(app) as client:
        response = await client.get(
            "/admin", headers=bearer(signing_key, roles=[{"role": "super_admin", "salon_id": None}])
        )

    assert response.status_code == 200


async def test_a_service_token_does_not_open_a_user_endpoint(
    app: FastAPI, signing_key: rsa.RSAPrivateKey
) -> None:
    async with app_client(app) as client:
        response = await client.get(
            "/me", headers=bearer(signing_key, typ=SERVICE_TOKEN_TYPE, sub="catalog")
        )

    # A service token has no user behind it, and an endpoint treating its sub
    # as a user id writes rows owned by a client id.
    assert response.status_code == 401


async def test_a_service_token_with_the_scope_opens_an_internal_endpoint(
    app: FastAPI, signing_key: rsa.RSAPrivateKey
) -> None:
    async with app_client(app) as client:
        response = await client.get(
            "/internal/v1/things",
            headers=bearer(
                signing_key,
                typ=SERVICE_TOKEN_TYPE,
                sub="booking",
                scopes=["catalog:read"],
                roles=[],
            ),
        )

    assert response.status_code == 200


async def test_a_service_token_without_the_scope_answers_403(
    app: FastAPI, signing_key: rsa.RSAPrivateKey
) -> None:
    async with app_client(app) as client:
        response = await client.get(
            "/internal/v1/things",
            headers=bearer(
                signing_key, typ=SERVICE_TOKEN_TYPE, sub="booking", scopes=["catalog:write"]
            ),
        )

    assert response.status_code == 403


async def test_a_user_token_never_opens_an_internal_endpoint(
    app: FastAPI, signing_key: rsa.RSAPrivateKey
) -> None:
    async with app_client(app) as client:
        response = await client.get(
            "/internal/v1/things",
            headers=bearer(signing_key, roles=[{"role": "super_admin", "salon_id": None}]),
        )

    # However privileged the customer is: endpoints under /internal are not
    # part of the public contract and a customer's token must not open one.
    assert response.status_code == 401


async def test_a_service_without_a_verifier_fails_loudly(
    signing_key: rsa.RSAPrivateKey,
) -> None:
    from barber_common.app import create_app

    router = APIRouter()

    @router.get("/me")
    async def me(caller: Annotated[Principal, Depends(current_user)]) -> dict[str, str]:
        return {"subject": caller.subject}

    application = create_app(
        BaseAppSettings(
            service_name="catalog",
            environment=Environment.TEST,
            database_dsn="postgresql+asyncpg://catalog:secret@localhost:5432/catalog",
            redis_dsn="redis://localhost:6379/0",
            kafka_bootstrap_servers="localhost:9092",
        ),
        routers=[router],
    )

    # Forgetting use_authentication is a programming error, not a request the
    # caller got wrong: it raises rather than answering, so it is found the
    # first time the endpoint is exercised instead of letting requests through.
    async with app_client(application) as client:
        with pytest.raises(RuntimeError, match="verifier"):
            await client.get("/me", headers=bearer(signing_key))
