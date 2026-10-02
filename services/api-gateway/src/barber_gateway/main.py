"""Entry point of the api-gateway service.

Single entry point: it verifies the JWT, limits the rate, stamps the
correlation id and proxies to the services behind it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from barber_common.app import create_app
from barber_common.auth import jwks_verifier, refreshing, use_authentication
from barber_common.cache import cache_from_dsn
from barber_common.http import ServiceClient, Timeouts
from barber_gateway.api.docs import router as docs_router
from barber_gateway.api.proxied import router as proxied_router
from barber_gateway.api.v1.router import router
from barber_gateway.clients.booking import BookingClient
from barber_gateway.clients.catalog import CatalogClient
from barber_gateway.clients.openapi import OpenApiClient
from barber_gateway.openapi import PlatformDocument
from barber_gateway.proxy import Proxy
from barber_gateway.rate_limit import Limit, LimitScope, RateLimitPolicy, SlidingWindowLimiter
from barber_gateway.routing import Upstream
from barber_gateway.settings import GatewaySettings

__all__ = ["build_proxy", "create_application", "rate_limit_policy"]


def create_application(settings: GatewaySettings | None = None) -> FastAPI:
    """Build the application. Called by uvicorn with ``--factory``.

    ``settings`` is an argument so a test can assemble the service without an
    environment behind it.
    """
    resolved = settings or GatewaySettings.load()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # The keys of auth, cached and refreshed. Fetched lazily, so auth being
        # down delays the first verification instead of stopping the start.
        jwks, verifier = jwks_verifier(resolved)

        async with AsyncExitStack() as stack:
            app.state.proxy = build_proxy(resolved)
            stack.push_async_callback(app.state.proxy.aclose)
            await stack.enter_async_context(refreshing(jwks))
            use_authentication(app, verifier)
            # Not part of the readiness probe, like the cache of the other
            # services: without Redis the limiter lets traffic through, so it
            # is no reason to leave the balancer. redis-py connects lazily.
            app.state.rate_limiter = SlidingWindowLimiter(
                cache_from_dsn(
                    str(resolved.redis_dsn.get_secret_value()),
                    timeout_seconds=resolved.rate_limit_timeout_seconds,
                )
            )
            stack.push_async_callback(app.state.rate_limiter.aclose)

            # The card of a master and the merged OpenAPI document read the
            # services as a client, with retries of their GETs and breakers of
            # their own, apart from the proxy.
            clients = {
                upstream: ServiceClient(base_url=base_url, upstream=upstream.value)
                for upstream, base_url in _base_urls(resolved).items()
            }
            for http in clients.values():
                stack.push_async_callback(http.aclose)
            app.state.catalog = CatalogClient(http=clients[Upstream.CATALOG])
            app.state.booking = BookingClient(http=clients[Upstream.BOOKING])
            app.state.openapi = PlatformDocument(
                gateway=app.openapi,
                documents=OpenApiClient(http=clients),
                ttl_seconds=resolved.openapi_cache_ttl_seconds,
            )
            yield

    application = create_app(
        resolved,
        # The router of the gateway's own endpoints comes first: the proxy
        # takes every /api path that reaches it.
        routers=[docs_router, router, proxied_router],
        lifespan=lifespan,
        title="Barber API Gateway",
        # The gateway serves the document of the whole platform instead, at the
        # same address; see api/docs.py.
        openapi_url=None,
    )
    application.state.rate_limits = rate_limit_policy(resolved)
    return application


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


def rate_limit_policy(settings: GatewaySettings) -> RateLimitPolicy:
    """The limits of docs/04, with the numbers of the settings."""
    return RateLimitPolicy(
        anonymous=Limit(LimitScope.ANONYMOUS, settings.rate_limit_anonymous_per_minute, 60),
        user=Limit(LimitScope.USER, settings.rate_limit_user_per_minute, 60),
        booking_create=Limit(
            LimitScope.BOOKING_CREATE, settings.rate_limit_booking_create_per_minute, 60
        ),
        registration=Limit(
            LimitScope.REGISTRATION, settings.rate_limit_registration_per_hour, 3600
        ),
    )


def _base_urls(settings: GatewaySettings) -> dict[Upstream, str]:
    return {
        Upstream.AUTH: settings.auth_url,
        Upstream.CATALOG: settings.catalog_url,
        Upstream.BOOKING: settings.booking_url,
        Upstream.NOTIFICATION: settings.notification_url,
    }
