"""Fixtures of the api-gateway suite.

The services behind the gateway are other processes, so they are the external
boundary here and the one thing replaced: an ``httpx.MockTransport`` stands in
for all four, told apart by the host of the request. Everything in front of the
transport -- routing, headers, error handling, the middleware of the chassis --
is the real code.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI

from barber_common.auth import ACCESS_TOKEN_TYPE, StaticKeys, TokenVerifier, use_authentication
from barber_common.config import Environment
from barber_common.http import RetryPolicy, ServiceClient, Timeouts
from barber_common.testing.fixtures import app_client
from barber_gateway.clients.booking import BookingClient
from barber_gateway.clients.catalog import CatalogClient
from barber_gateway.clients.openapi import OpenApiClient
from barber_gateway.main import create_application
from barber_gateway.openapi import PlatformDocument
from barber_gateway.proxy import Proxy
from barber_gateway.rate_limit import SlidingWindowLimiter
from barber_gateway.routing import Upstream
from barber_gateway.settings import GatewaySettings

UPSTREAM_URLS = {upstream: f"http://{upstream.value}" for upstream in Upstream}

ServiceHandler = Callable[[httpx.Request], Awaitable[httpx.Response]]
# Builds the Authorization header of a caller; keyword arguments override claims.
BearerFactory = Callable[..., dict[str, str]]

ISSUER = "https://barber.local/auth"
KID = "gateway-test-key"
# The smallest size the verifier accepts; generated per run, never stored.
TEST_KEY_SIZE_BITS = 2048


def build_settings(**overrides: object) -> GatewaySettings:
    values: dict[str, object] = {
        "environment": Environment.TEST,
        "redis_dsn": "redis://localhost:6379/0",
        "kafka_bootstrap_servers": "localhost:9092",
        "auth_url": UPSTREAM_URLS[Upstream.AUTH],
        "catalog_url": UPSTREAM_URLS[Upstream.CATALOG],
        "booking_url": UPSTREAM_URLS[Upstream.BOOKING],
        "notification_url": UPSTREAM_URLS[Upstream.NOTIFICATION],
        "jwks_url": UPSTREAM_URLS[Upstream.AUTH],
    }
    values.update(overrides)
    return GatewaySettings.model_validate(values)


class FakeServices:
    """The four services, as one transport that records what reached them.

    Unless a test says otherwise, a service echoes the request back as JSON,
    which is what most routing and header assertions want to look at.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._handlers: dict[str, ServiceHandler] = {}

    def answer(self, upstream: Upstream, handler: ServiceHandler) -> None:
        """Make one service answer with this handler from now on."""
        self._handlers[upstream.value] = handler

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def reached(self, upstream: Upstream) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.host == upstream.value]

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self._handlers.get(request.url.host, _echo)
        response = await handler(request)
        if not response.is_stream_consumed:
            return response
        # A response built from bytes or JSON is read on construction, and a
        # real transport never hands one over that way: the proxy streams the
        # body and would find nothing left. Served as a stream, as on the wire.
        return httpx.Response(
            response.status_code,
            headers=response.headers.multi_items(),
            content=_as_stream(response.content),
        )


async def _as_stream(content: bytes) -> AsyncIterator[bytes]:
    yield content


async def _echo(request: httpx.Request) -> httpx.Response:
    body = await request.aread()
    return httpx.Response(
        200,
        json={
            "upstream": request.url.host,
            "method": request.method,
            "path": request.url.raw_path.decode("ascii").split("?")[0],
            "query": request.url.query.decode("ascii"),
            "body": body.decode("utf-8"),
        },
    )


@pytest.fixture
def settings() -> GatewaySettings:
    return build_settings()


@pytest.fixture
def services() -> FakeServices:
    return FakeServices()


@pytest.fixture
def proxy(services: FakeServices) -> Proxy:
    return Proxy(base_urls=UPSTREAM_URLS, timeouts=Timeouts(), transport=services.transport())


@pytest.fixture(scope="session")
def signing_key() -> rsa.RSAPrivateKey:
    """The throwaway key this run signs its tokens with."""
    return rsa.generate_private_key(public_exponent=65537, key_size=TEST_KEY_SIZE_BITS)


@pytest.fixture
def bearer(signing_key: rsa.RSAPrivateKey) -> BearerFactory:
    """Authorization headers of callers; a client of the platform by default."""

    def build(*, key: rsa.RSAPrivateKey | None = None, **overrides: object) -> dict[str, str]:
        now = datetime.now(UTC)
        claims: dict[str, object] = {
            "sub": str(uuid4()),
            "typ": ACCESS_TOKEN_TYPE,
            "roles": [{"role": "client", "salon_id": None}],
            "iss": ISSUER,
            "jti": str(uuid4()),
            "iat": now,
            "exp": now + timedelta(minutes=15),
        }
        claims.update(overrides)
        token = jwt.encode(claims, key or signing_key, algorithm="RS256", headers={"kid": KID})
        return {"Authorization": f"Bearer {token}"}

    return build


@pytest.fixture
def app(
    settings: GatewaySettings,
    proxy: Proxy,
    services: FakeServices,
    signing_key: rsa.RSAPrivateKey,
) -> FastAPI:
    """The real gateway, with the fake services wired in place of the lifespan.

    Tokens are real and really verified, against the public half of the key
    this run signs with.
    """
    application = create_application(settings)
    application.state.proxy = proxy
    # Admits everything; the suite of the limiter puts a real one in its place.
    application.state.rate_limiter = SlidingWindowLimiter.disabled()
    application.state.catalog = CatalogClient(http=_service_client(Upstream.CATALOG, services))
    application.state.booking = BookingClient(http=_service_client(Upstream.BOOKING, services))
    application.state.openapi = PlatformDocument(
        gateway=application.openapi,
        documents=OpenApiClient(
            http={upstream: _service_client(upstream, services) for upstream in Upstream}
        ),
        ttl_seconds=settings.openapi_cache_ttl_seconds,
    )
    public_pem = (
        signing_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    use_authentication(
        application,
        TokenVerifier(keys=StaticKeys.from_pem(kid=KID, public_pem=public_pem), issuer=ISSUER),
    )
    return application


def _service_client(upstream: Upstream, services: FakeServices) -> ServiceClient:
    """A client of one fake service, retrying without the pauses."""
    return ServiceClient(
        base_url=UPSTREAM_URLS[upstream],
        upstream=upstream.value,
        retry=RetryPolicy(initial_delay_seconds=0.0),
        transport=services.transport(),
    )


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A stranger: no token."""
    async with app_client(app) as client:
        yield client


@pytest.fixture
async def user_client(app: FastAPI, bearer: BearerFactory) -> AsyncIterator[httpx.AsyncClient]:
    """A signed-in client of the platform, for requests about something else."""
    async with app_client(app) as client:
        client.headers.update(bearer())
        yield client
