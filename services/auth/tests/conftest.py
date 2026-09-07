"""Fixtures of the auth suite.

The database is created once per session and brought up by the real
migrations -- the same scripts the migration Job runs. Nothing here builds a
schema from the models: a schema that never went through Alembic leaves the
migrations untested until the first deployment.

Isolation is a rollback. Every test gets sessions bound to one connection with
an open transaction, and whatever it writes disappears when it ends, so two
tests writing to ``users`` never see each other.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from barber_auth.adapters.argon2_hasher import Argon2Hasher
from barber_auth.adapters.dev_mailer import DevMailer
from barber_auth.main import create_application
from barber_auth.settings import ALEMBIC_INI, AuthSettings
from barber_common.config import Environment
from barber_common.db import create_engine, get_session
from barber_common.testing.fixtures import (
    apply_migrations,
    create_database,
    isolated_session_factory,
)

DATABASE_NAME = "auth_test"

# A DSN that points nowhere: the tests that do not touch a database get their
# settings from here, and the ones that do override it with the container.
PLACEHOLDER_DSN = "postgresql+asyncpg://auth:secret@localhost:5432/auth"


def build_settings(**overrides: object) -> AuthSettings:
    """Settings of the service under test, with no environment behind them.

    argon2 is deliberately cheap here. The production parameters spend 64 MiB
    and tens of milliseconds per hash, and a suite that registers users would
    pay that on every test for a property none of them assert.
    """
    fields: dict[str, object] = {
        "environment": Environment.TEST,
        "database_dsn": PLACEHOLDER_DSN,
        "redis_dsn": "redis://localhost:6379/0",
        "kafka_bootstrap_servers": "localhost:9092",
        "password_argon2_time_cost": 1,
        "password_argon2_memory_kib": 8,
        "password_argon2_parallelism": 1,
    }
    fields.update(overrides)
    return AuthSettings(**fields)  # type: ignore[arg-type]  # settings fields are typed per key


@pytest.fixture(scope="session")
def settings() -> AuthSettings:
    return build_settings()


@pytest.fixture(scope="session")
def auth_dsn(postgres_dsn: str) -> str:
    """A database of its own for this suite, migrated to head."""
    dsn = create_database(postgres_dsn, DATABASE_NAME)
    apply_migrations(dsn=dsn, alembic_ini=ALEMBIC_INI)
    return dsn


@pytest.fixture
async def engine(auth_dsn: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(auth_dsn, pool_size=5, max_overflow=5)

    yield engine

    await engine.dispose()


@pytest.fixture
async def session_factory(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions whose writes are rolled back when the test ends."""
    async with isolated_session_factory(engine) as factory:
        yield factory


@pytest.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """One session for a test that talks to the repositories directly."""
    async with session_factory() as session:
        yield session


@pytest.fixture
def hasher(settings: AuthSettings) -> Argon2Hasher:
    return Argon2Hasher(settings)


@pytest.fixture
def mailer(settings: AuthSettings) -> DevMailer:
    return DevMailer(
        environment=settings.environment,
        confirmation_url=settings.email_confirmation_url,
    )


@pytest.fixture
def app(
    settings: AuthSettings,
    session_factory: async_sessionmaker[AsyncSession],
) -> Iterator[FastAPI]:
    """The real application, wired to the rolled back database of the test.

    The lifespan does not run -- there is no broker and no schema check here --
    so the session dependency is pointed at the isolated factory instead of at
    ``app.state.database``. Everything else is production code: the routers,
    the middleware, the error handlers and the scenarios behind them.
    """
    application = create_application(settings)

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_get_session

    yield application

    application.dependency_overrides.clear()
