"""GET /.well-known/jwks.json through the real ASGI stack."""

from __future__ import annotations

from collections.abc import Callable

import jwt
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from barber_auth.adapters.rsa_signer import RsaTokenSigner
from barber_auth.models.signing_key import SigningKey
from barber_common.testing.fixtures import app_client

pytestmark = pytest.mark.integration

JWKS = "/.well-known/jwks.json"

# Every private member of an RSA JWK by the name RFC 7518 gives it. None may
# ever appear in this response, and this list is what the assertion checks
# against rather than a hand-picked "d".
PRIVATE_JWK_MEMBERS = ("d", "p", "q", "dp", "dq", "qi", "oth")


async def test_the_active_key_is_published(
    app: FastAPI, registered_signing_key: str, signer: RsaTokenSigner
) -> None:
    async with app_client(app) as client:
        response = await client.get(JWKS)

    assert response.status_code == 200
    keys = response.json()["keys"]
    assert [key["kid"] for key in keys] == [registered_signing_key]
    assert keys[0]["kid"] == signer.kid


async def test_a_key_carries_what_rfc_7517_requires(
    app: FastAPI, registered_signing_key: str
) -> None:
    async with app_client(app) as client:
        response = await client.get(JWKS)

    key = response.json()["keys"][0]

    assert key["kty"] == "RSA"
    assert key["use"] == "sig"
    assert key["alg"] == "RS256"
    assert set(key) == {"kty", "n", "e", "kid", "use", "alg"}


async def test_no_private_material_is_ever_served(
    app: FastAPI, registered_signing_key: str
) -> None:
    async with app_client(app) as client:
        response = await client.get(JWKS)

    # Both checks matter: the members, and the bytes. A key smuggled in as a
    # PEM under some other name would pass the first and fail the second.
    for key in response.json()["keys"]:
        assert not set(key) & set(PRIVATE_JWK_MEMBERS)
    assert "PRIVATE KEY" not in response.text


async def test_a_token_of_this_service_verifies_against_the_published_key(
    app: FastAPI, registered_signing_key: str, signer: RsaTokenSigner
) -> None:
    token = signer.sign({"sub": "ivan"})

    async with app_client(app) as client:
        response = await client.get(JWKS)

    # The whole point of the endpoint: a consumer picks the key by the kid in
    # the header and checks the signature without asking auth anything.
    key_id = jwt.get_unverified_header(token)["kid"]
    published = next(key for key in response.json()["keys"] if key["kid"] == key_id)
    verified = jwt.decode(token, jwt.PyJWK.from_dict(published).key, algorithms=["RS256"])
    assert verified["sub"] == "ivan"


async def test_both_keys_are_served_during_a_rotation(
    app: FastAPI,
    session: AsyncSession,
    registered_signing_key: str,
    make_private_key_pem: Callable[[], str],
) -> None:
    outgoing = RsaTokenSigner(make_private_key_pem())
    session.add(SigningKey(kid=outgoing.kid, public_pem=outgoing.public_pem, is_active=True))
    await session.commit()

    async with app_client(app) as client:
        response = await client.get(JWKS)

    # Both keys are published until the old tokens expire, so a rotation does
    # not break tokens issued just before it.
    assert {key["kid"] for key in response.json()["keys"]} == {
        registered_signing_key,
        outgoing.kid,
    }


async def test_a_retired_key_is_not_served(
    app: FastAPI,
    session: AsyncSession,
    registered_signing_key: str,
    make_private_key_pem: Callable[[], str],
) -> None:
    retired = RsaTokenSigner(make_private_key_pem())
    session.add(SigningKey(kid=retired.kid, public_pem=retired.public_pem, is_active=False))
    await session.commit()

    async with app_client(app) as client:
        response = await client.get(JWKS)

    assert {key["kid"] for key in response.json()["keys"]} == {registered_signing_key}


async def test_the_endpoint_needs_no_credentials(app: FastAPI, registered_signing_key: str) -> None:
    async with app_client(app) as client:
        response = await client.get(JWKS)

    # It has to be anonymous: a service cannot present a token issued by the
    # service whose keys it is fetching in order to verify that token.
    assert response.status_code == 200


async def test_the_endpoint_is_documented_in_the_openapi_contract(app: FastAPI) -> None:
    async with app_client(app) as client:
        document = (await client.get("/openapi.json")).json()

    # Not under /api/v1: the path is reserved by RFC 8615 and named by the
    # specification that defines it.
    assert JWKS in document["paths"]
