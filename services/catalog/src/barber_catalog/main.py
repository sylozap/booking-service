"""Entry point of the catalog service.

The shop window: salons, masters, services and the salon policies.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from barber_catalog.api.v1 import internal
from barber_catalog.api.v1.router import router
from barber_catalog.settings import ALEMBIC_INI, CatalogSettings
from barber_common.app import create_app, use_database
from barber_common.auth import jwks_verifier, refreshing, use_authentication
from barber_common.cache import Cache, cache_from_dsn
from barber_common.db import Database, check_schema_is_current, load_config
from barber_common.db.engine import create_engine_from_settings
from barber_common.kafka import EventProducer
from barber_common.outbox import OutboxRelay

__all__ = ["create_application"]


def create_application(settings: CatalogSettings | None = None) -> FastAPI:
    """Build the application. Called by uvicorn with ``--factory``.

    ``settings`` is an argument so a test can assemble the service without an
    environment behind it.
    """
    resolved = settings or CatalogSettings.load()

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

        # Tokens are verified in this service against the public keys of auth.
        jwks, verifier = jwks_verifier(resolved)

        # Not part of the readiness probe: reads fall through to the database
        # when Redis is unavailable.
        app.state.cache = _build_cache(resolved)

        async with AsyncExitStack() as stack:
            # The producer is not started here: the relay connects on its first
            # pass, so a broker that is down delays events instead of stopping
            # the service.
            stack.push_async_callback(producer.stop)
            await stack.enter_async_context(relay.run_in_background())
            # The keys are fetched lazily and refreshed on a timer, so auth
            # being down at this moment delays the first verification instead
            # of stopping the service from starting.
            await stack.enter_async_context(refreshing(jwks))
            stack.push_async_callback(app.state.cache.aclose)
            use_authentication(app, verifier)
            yield

    # Two routers. /internal/v1 is not part of the public contract, is not
    # published through the gateway, and opens only to a service token, so it
    # is mounted beside the public prefix rather than inside it.
    return create_app(
        resolved,
        routers=[router, internal.router],
        lifespan=lifespan,
        title="Barber Catalog",
    )


def _build_cache(settings: CatalogSettings) -> Cache:
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
