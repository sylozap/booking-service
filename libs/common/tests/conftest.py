"""Fixtures of the chassis tests.

The database of these tests is created once per session and brought up by the
real migrations -- the same reusable revision every service uses. Nothing here
builds a schema from the models: a schema that never went through Alembic
leaves the migrations untested until the first deployment.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from barber_common.config import BaseAppSettings, Environment
from barber_common.db import Database, create_engine
from barber_common.testing.fixtures import (
    apply_migrations,
    create_database,
    isolated_session_factory,
)

ALEMBIC_INI = Path(__file__).parent / "alembic.ini"
DATABASE_NAME = "chassis_test"

# Everything the shared revision creates. Truncated between the tests that
# commit for real; the rest are isolated by a rollback and need no cleaning.
SHARED_TABLES = ("outbox", "processed_events")


@pytest.fixture(scope="session")
def chassis_dsn(postgres_dsn: str) -> str:
    """A database of its own for the chassis suite, migrated to head."""
    dsn = create_database(postgres_dsn, DATABASE_NAME)
    apply_migrations(dsn=dsn, alembic_ini=ALEMBIC_INI)
    return dsn


@pytest.fixture
async def engine(chassis_dsn: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(chassis_dsn, pool_size=5, max_overflow=5)

    yield engine

    await engine.dispose()


@pytest.fixture
async def session_factory(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions whose writes are rolled back when the test ends.

    The default. A test that needs two transactions at the same time -- the
    ``SKIP LOCKED`` test does -- takes ``concurrent_session_factory`` instead,
    because savepoints on one connection are not two transactions.
    """
    async with isolated_session_factory(engine) as factory:
        yield factory


@pytest.fixture
async def concurrent_session_factory(
    engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions on connections of their own, cleaned up by truncating."""
    await _truncate(engine)

    yield Database(engine).session_factory

    await _truncate(engine)


async def _truncate(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {', '.join(SHARED_TABLES)}"))


@pytest.fixture
def settings() -> BaseAppSettings:
    """Settings of a service under test, with no environment behind them."""
    return BaseAppSettings(
        service_name="booking",
        environment=Environment.TEST,
        database_dsn="postgresql+asyncpg://booking:secret@localhost:5432/booking",
        redis_dsn="redis://localhost:6379/0",
        kafka_bootstrap_servers="localhost:9092",
    )
