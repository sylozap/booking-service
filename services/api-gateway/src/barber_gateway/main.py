"""Entry point of the api-gateway service.

Single entry point: it verifies the JWT, limits the rate, stamps the
correlation id and proxies to the services behind it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from barber_common.app import create_app
from barber_common.http import Timeouts
from barber_gateway.api.proxied import router as proxied_router
from barber_gateway.api.v1.router import router
from barber_gateway.proxy import Proxy
from barber_gateway.routing import Upstream
from barber_gateway.settings import GatewaySettings

__all__ = ["build_proxy", "create_application"]


def create_application(settings: GatewaySettings | None = None) -> FastAPI:
    """Build the application. Called by uvicorn with ``--factory``.

    ``settings`` is an argument so a test can assemble the service without an
    environment behind it.
    """
    resolved = settings or GatewaySettings.load()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            app.state.proxy = build_proxy(resolved)
            stack.push_async_callback(app.state.proxy.aclose)
            yield

    return create_app(
        resolved,
        # The router of the gateway's own endpoints comes first: the proxy
        # takes every /api path that reaches it.
        routers=[router, proxied_router],
        lifespan=lifespan,
        title="Barber API Gateway",
    )


def build_proxy(settings: GatewaySettings) -> Proxy:
    """The proxy to every service, with the deadlines of the settings."""
    return Proxy(
        base_urls=_base_urls(settings),
        timeouts=Timeouts(
            connect_seconds=settings.proxy_connect_timeout_seconds,
            read_seconds=settings.proxy_read_timeout_seconds,
            write_seconds=settings.proxy_write_timeout_seconds,
            pool_seconds=settings.proxy_pool_timeout_seconds,
        ),
    )


def _base_urls(settings: GatewaySettings) -> dict[Upstream, str]:
    return {
        Upstream.AUTH: settings.auth_url,
        Upstream.CATALOG: settings.catalog_url,
        Upstream.BOOKING: settings.booking_url,
        Upstream.NOTIFICATION: settings.notification_url,
    }
