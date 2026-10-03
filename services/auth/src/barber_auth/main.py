"""Entry point of the auth service.

Registration, passwords, JWT issuing and rotation, roles and JWKS.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from barber_auth.adapters.argon2_hasher import Argon2Hasher
from barber_auth.adapters.database_keys import DatabaseKeys
from barber_auth.adapters.rsa_signer import RsaTokenSigner
from barber_auth.api.v1 import jwks, service_tokens
from barber_auth.api.v1.router import router
from barber_auth.services.bootstrap import EnsureBootstrapAdmin
from barber_auth.services.keys import RegisterSigningKey
from barber_auth.services.service_tokens import RegisterServiceClients
from barber_auth.settings import ALEMBIC_INI, AuthSettings
from barber_auth.workers.cleanup import CleanupWorker
from barber_common.app import create_app, use_database
from barber_common.auth import TokenVerifier, use_authentication
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

        # Before the first token is signed: a token whose kid is missing from
        # JWKS is one no service can verify, so the public half is published
        # first and the ordering here is the guarantee of that.
        async with database.unit_of_work() as session:
            await RegisterSigningKey(session=session, signer=app.state.signer).execute()

        # Internal callers come from configuration rather than from a
        # migration: a client seeded by a migration carries its secret hash in
        # git, and rotating it becomes a schema change.
        async with database.unit_of_work() as session:
            await RegisterServiceClients(
                session=session,
                hasher=app.state.password_hasher,
                clients=resolved.service_clients,
            ).execute()

        # The first super_admin, the one account the API cannot create. A bare
        # session: the scenario opens its own transactions, so that argon2
        # runs outside them and a lost race with another replica is retried.
        async with database.session_factory() as session:
            await EnsureBootstrapAdmin(
                session=session,
                hasher=app.state.password_hasher,
                admin=resolved.bootstrap_admin(),
                password_min_length=resolved.password_min_length,
            ).execute()

        # auth verifies its own tokens against signing_keys directly rather
        # than calling its own JWKS endpoint.
        use_authentication(
            app,
            TokenVerifier(
                keys=DatabaseKeys(database.session_factory),
                issuer=resolved.jwt_issuer,
                leeway_seconds=resolved.jwt_leeway_seconds,
            ),
        )

        producer = EventProducer(
            bootstrap_servers=resolved.kafka_bootstrap_servers,
            service_name=resolved.service_name,
        )
        relay = OutboxRelay(session_factory=database.session_factory, producer=producer)
        cleanup = CleanupWorker(
            session_factory=database.session_factory,
            token_retention_days=resolved.cleanup_token_retention_days,
            confirmation_retention_days=resolved.cleanup_confirmation_retention_days,
            processed_event_retention_days=resolved.cleanup_processed_event_retention_days,
            batch_size=resolved.cleanup_batch_size,
            interval_seconds=resolved.cleanup_interval_seconds,
        )

        async with AsyncExitStack() as stack:
            # The producer is not started here: the relay connects on its first
            # pass, so a broker that is down delays events instead of stopping
            # the service.
            stack.push_async_callback(producer.stop)
            await stack.enter_async_context(relay.run_in_background())
            await stack.enter_async_context(cleanup.run_in_background())
            yield

    # Two routers outside the public prefix. /.well-known is reserved by
    # RFC 8615 and named by the specification; /internal/v1 is not part of the
    # public contract and is not published through the gateway.
    app = create_app(
        resolved,
        routers=[router, jwks.router, service_tokens.router],
        lifespan=lifespan,
        title="Barber Auth",
    )
    install_dependencies(app, resolved)
    return app


def install_dependencies(app: FastAPI, settings: AuthSettings) -> None:
    """Put the singletons of the service where the routers look for them.

    Built at assembly rather than in the lifespan, so an application that is
    never started still works in tests, and an unreadable private key stops the
    process immediately.
    """
    app.state.password_hasher = Argon2Hasher(settings)
    app.state.signer = RsaTokenSigner(settings.signing_key_pem())
