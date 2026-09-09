"""Entry point of the catalog service.

The shop window: salons, masters, services and the salon policies.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from barber_catalog.api.v1.router import router
from barber_catalog.settings import ALEMBIC_INI, CatalogSettings
from barber_common.app import create_app, use_database
from barber_common.auth import jwks_verifier, refreshing, use_authentication
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

        # Every service checks the tokens it receives itself, against the
        # public keys of auth rather than against a header the gateway set: a
        # request reaching this pod directly is refused by this pod (ADR-0010).
        jwks, verifier = jwks_verifier(resolved)

        async with AsyncExitStack() as stack:
            # The producer is deliberately not started here. The relay connects
            # on its first pass, so a broker that is down delays the events
            # instead of stopping the service (docs/08-consistency.md).
            stack.push_async_callback(producer.stop)
            await stack.enter_async_context(relay.run_in_background())
            # The keys are fetched lazily and refreshed on a timer, so auth
            # being down at this moment delays the first verification instead
            # of stopping the service from starting.
            await stack.enter_async_context(refreshing(jwks))
            use_authentication(app, verifier)
            yield

    return create_app(resolved, routers=[router], lifespan=lifespan, title="Barber Catalog")
