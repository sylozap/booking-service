"""The notification schema, applied and removed by the real migration scripts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from alembic import command
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine

from barber_common.db import create_engine
from barber_common.db.migrations import load_config
from barber_common.testing.fixtures import create_database
from barber_notification.settings import ALEMBIC_INI

pytestmark = pytest.mark.integration

DATABASE_NAME = "notification_migrations_test"

NOTIFICATION_TABLES = {"recipients", "notifications"}
SHARED_TABLES = {"outbox", "processed_events"}


@pytest.fixture
def dsn(postgres_dsn: str) -> str:
    """A database of its own, so the downgrade here breaks no other test."""
    return create_database(postgres_dsn, DATABASE_NAME)


@pytest.fixture
async def migrations_engine(dsn: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(dsn, pool_size=2, max_overflow=0)

    yield engine

    await engine.dispose()


async def upgrade(dsn: str, revision: str = "head") -> None:
    # Alembic runs an event loop of its own, so it cannot run inside this one.
    await asyncio.to_thread(command.upgrade, load_config(ALEMBIC_INI, dsn=dsn), revision)


async def downgrade(dsn: str, revision: str = "base") -> None:
    await asyncio.to_thread(command.downgrade, load_config(ALEMBIC_INI, dsn=dsn), revision)


async def table_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as connection:
        return set(await connection.run_sync(lambda sync: inspect(sync).get_table_names()))


async def unique_names(engine: AsyncEngine, table: str) -> set[str]:
    async with engine.connect() as connection:
        constraints = await connection.run_sync(
            lambda sync: inspect(sync).get_unique_constraints(table)
        )
    return {str(constraint["name"]) for constraint in constraints}


async def test_upgrade_head_creates_the_whole_notification_schema(
    dsn: str, migrations_engine: AsyncEngine
) -> None:
    await upgrade(dsn)

    assert NOTIFICATION_TABLES | SHARED_TABLES <= await table_names(migrations_engine)


async def test_the_dedup_key_is_unique_in_the_database(
    dsn: str, migrations_engine: AsyncEngine
) -> None:
    await upgrade(dsn)

    assert "uq_notifications_dedup_key" in await unique_names(migrations_engine, "notifications")


async def test_downgrade_removes_the_notification_tables(
    dsn: str, migrations_engine: AsyncEngine
) -> None:
    await upgrade(dsn)

    await downgrade(dsn, "0001_shared_tables")

    assert NOTIFICATION_TABLES & await table_names(migrations_engine) == set()


async def test_the_schema_can_be_applied_again_after_a_full_downgrade(
    dsn: str, migrations_engine: AsyncEngine
) -> None:
    await upgrade(dsn)
    await downgrade(dsn)

    await upgrade(dsn)

    assert NOTIFICATION_TABLES <= await table_names(migrations_engine)
