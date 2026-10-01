"""Fixtures of the api-gateway suite.

The services behind the gateway are other processes, so they are the external
boundary here and the one thing replaced: an ``httpx.MockTransport`` stands in
for all four, told apart by the host of the request. Everything in front of the
transport -- routing, headers, error handling, the middleware of the chassis --
is the real code.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import httpx
import pytest
from fastapi import FastAPI

from barber_common.config import Environment
from barber_common.http import Timeouts
from barber_common.testing.fixtures import app_client
from barber_gateway.main import create_application
from barber_gateway.proxy import Proxy
from barber_gateway.routing import Upstream
from barber_gateway.settings import GatewaySettings

UPSTREAM_URLS = {upstream: f"http://{upstream.value}" for upstream in Upstream}

ServiceHandler = Callable[[httpx.Request], Awaitable[httpx.Response]]


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


@pytest.fixture
def app(settings: GatewaySettings, proxy: Proxy) -> FastAPI:
    """The real gateway, with the fake services wired in place of the lifespan."""
    application = create_application(settings)
    application.state.proxy = proxy
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with app_client(app) as client:
        yield client
