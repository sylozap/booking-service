"""POST /internal/v1/token, and what a service token may and may not open."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
import pytest
from fastapi import APIRouter, Depends, FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from barber_auth.adapters.argon2_hasher import Argon2Hasher
from barber_auth.adapters.rsa_signer import RsaTokenSigner, public_jwk_of_pem
from barber_auth.domain.roles import Role
from barber_auth.models.service_client import ServiceClient
from barber_auth.models.user import User
from barber_auth.services.service_tokens import RegisterServiceClients
from barber_auth.settings import AuthSettings, ServiceClientConfig
from barber_common.auth import Principal, require_service_token
from barber_common.testing.fixtures import app_client

pytestmark = pytest.mark.integration

TOKEN = "/internal/v1/token"

CLIENT_ID = "booking"
CLIENT_SECRET = "a-secret-only-booking-knows"
SCOPES = ("catalog:read",)

UserFactory = Callable[..., Awaitable[User]]
AuthorizationFactory = Callable[..., Awaitable[dict[str, str]]]


def credentials(**overrides: str) -> dict[str, str]:
    return {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET} | overrides


@pytest.fixture(scope="session")
def settings(make_settings: Callable[..., AuthSettings]) -> AuthSettings:
    """Settings with one internal caller configured.

    Overrides the suite-wide fixture for this file only: the clients are
    configuration, so a test about them has to configure them.
    """
    return make_settings(
        service_clients={
            CLIENT_ID: ServiceClientConfig(secret=CLIENT_SECRET, scopes=SCOPES),
        }
    )


def claims_of(token: str, signer: RsaTokenSigner) -> dict[str, object]:
    """Verify a token the way a receiving service would."""
    published = public_jwk_of_pem(signer.public_pem, kid=signer.kid)
    decoded = jwt.decode(token, jwt.PyJWK.from_dict(published).key, algorithms=["RS256"])
    assert isinstance(decoded, dict)
    return decoded


async def test_valid_credentials_return_a_token(
    app: FastAPI, registered_service_clients: int
) -> None:
    async with app_client(app) as client:
        response = await client.post(TOKEN, json=credentials())

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "Bearer"
    assert body["scopes"] == list(SCOPES)


async def test_the_token_says_it_belongs_to_a_service(
    app: FastAPI, registered_service_clients: int, signer: RsaTokenSigner
) -> None:
    async with app_client(app) as client:
        response = await client.post(TOKEN, json=credentials())

    claims = claims_of(response.json()["access_token"], signer)
    # One claim is what stops a customer's token from opening /internal.
    assert claims["typ"] == "service"
    assert claims["sub"] == CLIENT_ID
    assert claims["scopes"] == list(SCOPES)


async def test_the_token_carries_no_roles(
    app: FastAPI, registered_service_clients: int, signer: RsaTokenSigner
) -> None:
    async with app_client(app) as client:
        response = await client.post(TOKEN, json=credentials())

    # A service is not a person and has no place in a salon. Scopes and roles
    # are never mixed.
    assert "roles" not in claims_of(response.json()["access_token"], signer)


async def test_the_token_is_shorter_lived_than_a_user_token(
    app: FastAPI, registered_service_clients: int, settings: AuthSettings
) -> None:
    async with app_client(app) as client:
        response = await client.post(TOKEN, json=credentials())

    expires_at = datetime.fromisoformat(response.json()["expires_at"])
    lifetime = expires_at - datetime.now(UTC)
    assert lifetime < timedelta(minutes=settings.access_token_ttl_minutes)


async def test_no_refresh_token_is_issued(app: FastAPI, registered_service_clients: int) -> None:
    async with app_client(app) as client:
        response = await client.post(TOKEN, json=credentials())

    # The client holds its credentials permanently and asks again; a rotation
    # mechanism would protect a secret that never travels.
    assert "refresh_token" not in response.json()


async def test_a_wrong_secret_answers_401(app: FastAPI, registered_service_clients: int) -> None:
    async with app_client(app) as client:
        response = await client.post(TOKEN, json=credentials(client_secret="wrong"))

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


async def test_an_unknown_client_answers_exactly_like_a_wrong_secret(
    app: FastAPI, registered_service_clients: int
) -> None:
    async with app_client(app) as client:
        unknown = await client.post(TOKEN, json=credentials(client_id="nobody"))
        wrong = await client.post(TOKEN, json=credentials(client_secret="wrong"))

    # Any difference here lists the services of the platform to anyone who asks.
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["code"] == wrong.json()["code"]
    assert unknown.json()["detail"] == wrong.json()["detail"]


async def test_a_deactivated_client_stops_working(
    app: FastAPI, registered_service_clients: int, session: AsyncSession
) -> None:
    stored = (await session.execute(select(ServiceClient))).scalar_one()
    stored.is_active = False
    await session.commit()

    async with app_client(app) as client:
        response = await client.post(TOKEN, json=credentials())

    assert response.status_code == 401


async def test_the_secret_is_stored_only_as_a_hash(
    app: FastAPI, registered_service_clients: int, session: AsyncSession
) -> None:
    stored = (await session.execute(select(ServiceClient))).scalar_one()

    # A leaked table must not be a set of working credentials.
    assert stored.secret_hash != CLIENT_SECRET
    assert stored.secret_hash.startswith("$argon2")


async def test_the_response_never_quotes_the_secret(
    app: FastAPI, registered_service_clients: int
) -> None:
    async with app_client(app) as client:
        response = await client.post(TOKEN, json=credentials(client_secret="wrong-secret-here"))

    assert "wrong-secret-here" not in response.text


async def test_registering_twice_updates_rather_than_duplicates(
    session: AsyncSession, registered_service_clients: int, settings: AuthSettings
) -> None:
    await RegisterServiceClients(
        session=session, hasher=Argon2Hasher(settings), clients=settings.service_clients
    ).execute()

    # A redeploy with a rotated secret has to take effect, so the conflict
    # updates rather than does nothing -- unlike the signing key, whose kid is
    # derived from the key itself.
    assert len((await session.execute(select(ServiceClient))).scalars().all()) == 1


async def test_a_deactivated_client_is_not_switched_back_on_by_a_restart(
    session: AsyncSession, registered_service_clients: int, settings: AuthSettings
) -> None:
    stored = (await session.execute(select(ServiceClient))).scalar_one()
    stored.is_active = False
    await session.flush()

    await RegisterServiceClients(
        session=session, hasher=Argon2Hasher(settings), clients=settings.service_clients
    ).execute()

    await session.refresh(stored)
    # An operator switched this client off. A rolling restart must not undo it.
    assert stored.is_active is False


def with_internal_probe(app: FastAPI) -> FastAPI:
    """Add an endpoint closed by require_service_token, for the two tests below.

    A stand-in for the internal endpoints of catalog and booking, which do not
    exist yet: what is under test is the dependency and the token, not the
    endpoint behind them.
    """
    probe = APIRouter()

    @probe.get("/internal/v1/probe")
    async def internal(
        caller: Annotated[Principal, Depends(require_service_token("catalog:read"))],
    ) -> dict[str, str]:
        return {"subject": caller.subject}

    app.include_router(probe)
    return app


async def test_a_user_token_does_not_open_an_internal_endpoint(
    app_with_verifier: FastAPI, authorize: AuthorizationFactory
) -> None:
    with_internal_probe(app_with_verifier)
    headers = await authorize(roles=((Role.SUPER_ADMIN, None),))

    async with app_client(app_with_verifier) as client:
        response = await client.get("/internal/v1/probe", headers=headers)

    # However privileged the customer: a user token never opens /internal.
    assert response.status_code == 401


async def test_a_service_token_opens_an_internal_endpoint(
    app_with_verifier: FastAPI, registered_service_clients: int, registered_signing_key: str
) -> None:
    with_internal_probe(app_with_verifier)

    async with app_client(app_with_verifier) as client:
        issued = (await client.post(TOKEN, json=credentials())).json()
        response = await client.get(
            "/internal/v1/probe",
            headers={"Authorization": f"Bearer {issued['access_token']}"},
        )

    assert response.status_code == 200
    assert response.json() == {"subject": CLIENT_ID}


async def test_an_unknown_field_in_the_body_is_refused(app: FastAPI) -> None:
    async with app_client(app) as client:
        response = await client.post(TOKEN, json=credentials() | {"scopes": ["catalog:write"]})

    # Otherwise a caller could try to widen its own scopes by guessing a field
    # name, and the failure would be silent.
    assert response.status_code == 422


async def test_the_endpoint_is_documented_in_the_openapi_contract(app: FastAPI) -> None:
    async with app_client(app) as client:
        document = (await client.get("/openapi.json")).json()

    operation = document["paths"][TOKEN]["post"]
    assert set(operation["responses"]) >= {"200", "401"}


async def test_a_deployment_with_no_internal_callers_registers_nothing(
    session: AsyncSession, make_settings: Callable[..., AuthSettings]
) -> None:
    bare = make_settings()

    registered = await RegisterServiceClients(
        session=session, hasher=Argon2Hasher(bare), clients=bare.service_clients
    ).execute()

    assert registered == 0
    assert (await session.execute(select(ServiceClient))).scalars().all() == []
