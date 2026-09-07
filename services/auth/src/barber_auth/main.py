"""Entry point of the auth service.

Registration, passwords, JWT issuing and rotation, roles and JWKS.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from barber_auth.adapters.argon2_hasher import Argon2Hasher
from barber_auth.adapters.database_keys import DatabaseKeys
from barber_auth.adapters.dev_mailer import DevMailer
from barber_auth.adapters.rsa_signer import RsaTokenSigner
from barber_auth.api.v1 import jwks
from barber_auth.api.v1.router import router
from barber_auth.services.keys import RegisterSigningKey
from barber_auth.settings import ALEMBIC_INI, AuthSettings
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

        # auth verifies the tokens it issued itself, against signing_keys
        # rather than against its own JWKS endpoint: it holds the keys already,
        # and a service that calls itself over HTTP depends on its own
        # readiness in order to become ready.
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

        async with AsyncExitStack() as stack:
            # The producer is deliberately not started here. The relay connects
            # on its first pass, so a broker that is down delays the events
            # instead of stopping the service (docs/08-consistency.md).
            stack.push_async_callback(producer.stop)
            await stack.enter_async_context(relay.run_in_background())
            yield

    # The JWKS router is mounted at the root and not behind /api/v1: the path
    # is reserved by RFC 8615 and named by the specification, not by us.
    app = create_app(
        resolved,
        routers=[router, jwks.router],
        lifespan=lifespan,
        title="Barber Auth",
    )
    install_dependencies(app, resolved)
    return app


def install_dependencies(app: FastAPI, settings: AuthSettings) -> None:
    """Put the singletons of the service where the routers look for them.

    Built here rather than in the lifespan: none of them needs a connection,
    and a test that assembles the application without starting it still gets a
    working registration. The argon2 hasher is expensive to construct -- it
    builds the hash it verifies against when there is no user -- and there is
    one of it per process for that reason.

    The signer is built here too, which means a private key that cannot be read
    or is not RSA stops the process at assembly rather than at the first login.
    Reading it is the only moment the key material is handled; from then on it
    lives inside the signer.
    """
    app.state.password_hasher = Argon2Hasher(settings)
    app.state.signer = RsaTokenSigner(settings.signing_key_pem())
    app.state.mailer = DevMailer(
        environment=settings.environment,
        confirmation_url=settings.email_confirmation_url,
    )
