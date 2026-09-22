"""Entry point of the booking service.

Schedules, availability and the lifecycle of a booking. Holds the main
invariant of the platform: the bookings of one master never overlap.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from barber_booking.api.v1.router import router
from barber_booking.clients.catalog import CatalogClient
from barber_booking.clients.service_token import ServiceTokenProvider
from barber_booking.settings import ALEMBIC_INI, BookingSettings
from barber_common.app import create_app, use_database
from barber_common.auth import jwks_verifier, refreshing, use_authentication
from barber_common.cache import Cache, cache_from_dsn
from barber_common.db import Database, check_schema_is_current, load_config
from barber_common.db.engine import create_engine_from_settings
from barber_common.http import ServiceClient
from barber_common.kafka import EventProducer
from barber_common.outbox import OutboxRelay

__all__ = ["create_application"]


def create_application(settings: BookingSettings | None = None) -> FastAPI:
    """Build the application. Called by uvicorn with ``--factory``.

    ``settings`` is an argument so a test can assemble the service without an
    environment behind it.
    """
    resolved = settings or BookingSettings.load()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = Database(create_engine_from_settings(resolved))
        # Before a single request is served: a pod working against a schema of
        # the wrong version writes rows the next release cannot read.
        await check_schema_is_current(database.engine, load_config(ALEMBIC_INI))
        use_database(app, database)

        producer = EventProducer(
            bootstrap_servers=resolved.kafka_bootstrap_servers,
            service_name=resolved.service_name,
        )
        relay = OutboxRelay(session_factory=database.session_factory, producer=producer)

        # Every service checks the tokens it receives itself, against the
        # public keys of auth, not against a header the gateway set.
        jwks, verifier = jwks_verifier(resolved)

        # Not part of the readiness probe: a read falls through to catalog
        # without it, so Redis being down is no reason to leave the balancer.
        app.state.cache = _build_cache(resolved)

        auth_http = ServiceClient(base_url=resolved.auth_url, upstream="auth")
        catalog_http = ServiceClient(base_url=resolved.catalog_url, upstream="catalog")
        app.state.catalog = CatalogClient(
            http=catalog_http,
            tokens=ServiceTokenProvider(
                http=auth_http,
                client_id=resolved.service_client_id,
                client_secret=resolved.service_client_secret,
            ),
        )

        async with AsyncExitStack() as stack:
            # The producer is not started here: the relay connects on its first
            # pass, so a broker that is down delays events instead of stopping
            # the service.
            stack.push_async_callback(producer.stop)
            await stack.enter_async_context(relay.run_in_background())
            stack.push_async_callback(auth_http.aclose)
            stack.push_async_callback(catalog_http.aclose)
            stack.push_async_callback(app.state.cache.aclose)
            # Keys are fetched lazily, so auth being down delays the first
            # verification instead of stopping the start.
            await stack.enter_async_context(refreshing(jwks))
            use_authentication(app, verifier)
            yield

    return create_app(resolved, routers=[router], lifespan=lifespan, title="Barber Booking")


def _build_cache(settings: BookingSettings) -> Cache:
    """The cache of the running service, or one that stores nothing.

    No connection is made here: redis-py connects lazily, so a Redis that is
    down at startup delays nothing and the first read simply misses.
    """
    if not settings.cache_enabled:
        return Cache.disabled()

    return Cache(
        cache_from_dsn(
            str(settings.redis_dsn.get_secret_value()),
            timeout_seconds=settings.cache_timeout_seconds,
        ),
        ttl_seconds=settings.cache_ttl_seconds,
    )
