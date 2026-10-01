"""The gateway checks the token first, and vouches for the caller after."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from barber_common.auth import SERVICE_TOKEN_TYPE
from barber_common.errors import PROBLEM_CONTENT_TYPE
from barber_gateway.routing import Upstream

BearerFactory = Callable[..., dict[str, str]]

MASTER = "0192f3c1-6a2b-7c3d-8e4f-5a6b7c8d9e0f"
SALON = "0192f3c1-1111-7c3d-8e4f-5a6b7c8d9e0f"


class FakeServices(Protocol):
    """The fake services of the conftest, as far as these tests use them.

    A protocol rather than the class itself: pytest runs this suite in
    importlib mode, so ``tests.conftest`` is not an importable module.
    """

    requests: list[httpx.Request]

    def reached(self, upstream: Upstream) -> list[httpx.Request]: ...


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/bookings"),
        ("POST", "/api/v1/bookings"),
        ("POST", "/api/v1/salons"),
        ("PATCH", f"/api/v1/masters/{MASTER}"),
        ("GET", f"/api/v1/masters/{MASTER}/schedule"),
        ("GET", "/api/v1/notifications/preferences"),
        ("POST", f"/api/v1/users/{uuid4()}/roles"),
    ],
)
async def test_a_closed_path_without_a_token_is_refused_at_the_gateway(
    client: httpx.AsyncClient, services: FakeServices, method: str, path: str
) -> None:
    response = await client.request(method, path)

    assert response.status_code == 401
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    assert response.json()["code"] == "unauthorized"
    assert services.requests == []


@pytest.mark.parametrize(
    ("method", "path", "upstream"),
    [
        ("POST", "/api/v1/auth/register", Upstream.AUTH),
        ("POST", "/api/v1/auth/login", Upstream.AUTH),
        ("POST", "/api/v1/auth/refresh", Upstream.AUTH),
        ("POST", "/api/v1/auth/logout", Upstream.AUTH),
        ("POST", "/api/v1/auth/confirm-email", Upstream.AUTH),
        ("GET", "/.well-known/jwks.json", Upstream.AUTH),
        ("GET", "/api/v1/salons", Upstream.CATALOG),
        ("GET", f"/api/v1/salons/{SALON}/masters", Upstream.CATALOG),
        ("GET", f"/api/v1/services/{uuid4()}", Upstream.CATALOG),
        ("GET", f"/api/v1/masters/{MASTER}", Upstream.CATALOG),
        ("GET", "/api/v1/availability", Upstream.BOOKING),
    ],
)
async def test_a_public_path_is_open_to_a_stranger(
    client: httpx.AsyncClient,
    services: FakeServices,
    method: str,
    path: str,
    upstream: Upstream,
) -> None:
    response = await client.request(method, path)

    assert response.status_code == 200
    assert len(services.reached(upstream)) == 1


async def test_an_expired_token_is_refused(
    client: httpx.AsyncClient, services: FakeServices, bearer: BearerFactory
) -> None:
    an_hour_ago = datetime.now(UTC) - timedelta(hours=1)

    response = await client.get(
        "/api/v1/bookings", headers=bearer(iat=an_hour_ago, exp=an_hour_ago)
    )

    assert response.status_code == 401
    assert services.requests == []


async def test_a_token_signed_by_another_key_is_refused(
    client: httpx.AsyncClient, services: FakeServices, bearer: BearerFactory
) -> None:
    stranger = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    response = await client.get("/api/v1/bookings", headers=bearer(key=stranger))

    assert response.status_code == 401
    assert services.requests == []


async def test_a_service_token_is_refused_from_outside(
    client: httpx.AsyncClient, services: FakeServices, bearer: BearerFactory
) -> None:
    response = await client.get(
        "/api/v1/bookings", headers=bearer(typ=SERVICE_TOKEN_TYPE, roles=[], scopes=["x"])
    )

    assert response.status_code == 401
    assert services.requests == []


@pytest.mark.parametrize("authorization", ["Basic dXNlcjpwYXNz", "Bearer", "Bearer not-a-jwt"])
async def test_a_malformed_authorization_is_refused(
    client: httpx.AsyncClient, services: FakeServices, authorization: str
) -> None:
    response = await client.get("/api/v1/bookings", headers={"Authorization": authorization})

    assert response.status_code == 401
    assert services.requests == []


async def test_an_invalid_token_is_refused_on_a_public_path_too(
    client: httpx.AsyncClient, services: FakeServices, bearer: BearerFactory
) -> None:
    an_hour_ago = datetime.now(UTC) - timedelta(hours=1)

    response = await client.get("/api/v1/salons", headers=bearer(iat=an_hour_ago, exp=an_hour_ago))

    assert response.status_code == 401
    assert services.requests == []


async def test_the_identity_of_the_caller_reaches_the_service(
    client: httpx.AsyncClient, services: FakeServices, bearer: BearerFactory
) -> None:
    user_id = str(uuid4())
    headers = bearer(
        sub=user_id,
        roles=[{"role": "client", "salon_id": None}, {"role": "salon_admin", "salon_id": SALON}],
    )

    response = await client.get("/api/v1/bookings", headers=headers)

    assert response.status_code == 200
    forwarded = services.reached(Upstream.BOOKING)[0].headers
    assert forwarded["x-user-id"] == user_id
    assert forwarded["x-roles"] == f"client,salon_admin:{SALON}"
    # The service verifies the token itself, so it gets the token as sent.
    assert forwarded["authorization"] == headers["Authorization"]


async def test_an_identity_claimed_by_the_client_is_overwritten(
    client: httpx.AsyncClient, services: FakeServices, bearer: BearerFactory
) -> None:
    user_id = str(uuid4())

    await client.get(
        "/api/v1/bookings",
        headers={
            **bearer(sub=user_id),
            "X-User-Id": str(uuid4()),
            "X-Roles": "super_admin",
        },
    )

    forwarded = services.reached(Upstream.BOOKING)[0].headers
    assert forwarded.get_list("x-user-id") == [user_id]
    assert forwarded.get_list("x-roles") == ["client"]


async def test_an_identity_claimed_by_a_stranger_is_dropped(
    client: httpx.AsyncClient, services: FakeServices
) -> None:
    await client.get(
        "/api/v1/salons", headers={"X-User-Id": str(uuid4()), "X-Roles": "super_admin"}
    )

    forwarded = services.reached(Upstream.CATALOG)[0].headers
    assert "x-user-id" not in forwarded
    assert "x-roles" not in forwarded
