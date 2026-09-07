"""Entry point of the auth service.

Registration, passwords, JWT issuing and rotation, roles and JWKS.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from barber_auth.adapters.argon2_hasher import Argon2Hasher
from barber_auth.adapters.dev_mailer import DevMailer
from barber_auth.api.v1.router import router
from barber_auth.settings import ALEMBIC_INI, AuthSettings
from barber_common.app import create_app, use_database
from barber_common.db import Database, check_schema_is_current, load_config
from barber_common.db.engine import create_engine_from_settings
from barber_common.kafka import EventProducer
from barber_common.outbox import OutboxRelay

__all__ = ["create_application"]


def create_application(settings: AuthSettings | None = None) -> FastAPI:
    """Build the application. Called by uvicorn with ``--factory``.

    ``settings`` is an argument so a test can assemble the service without an
    environment behind it.
    """
    resolved = settings or AuthSettings.load()

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

        async with AsyncExitStack() as stack:
            # The producer is deliberately not started here. The relay connects
            # on its first pass, so a broker that is down delays the events
            # instead of stopping the service (docs/08-consistency.md).
            stack.push_async_callback(producer.stop)
            await stack.enter_async_context(relay.run_in_background())
            yield

    app = create_app(resolved, routers=[router], lifespan=lifespan, title="Barber Auth")
    install_dependencies(app, resolved)
    return app


def install_dependencies(app: FastAPI, settings: AuthSettings) -> None:
    """Put the singletons of the service where the routers look for them.

    Built here rather than in the lifespan: neither needs a connection, and a
    test that assembles the application without starting it still gets a
    working registration. The argon2 hasher is expensive to construct -- it
    builds the hash it verifies against when there is no user -- and there is
    one of it per process for that reason.
    """
    app.state.password_hasher = Argon2Hasher(settings)
    app.state.mailer = DevMailer(
        environment=settings.environment,
        confirmation_url=settings.email_confirmation_url,
    )
