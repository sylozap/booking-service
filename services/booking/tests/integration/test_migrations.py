"""The booking schema, applied and removed by the real migration scripts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from alembic import command
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine

from barber_booking.settings import ALEMBIC_INI
from barber_common.db import create_engine
from barber_common.db.migrations import load_config
from barber_common.testing.fixtures import create_database

pytestmark = pytest.mark.integration

DATABASE_NAME = "booking_migrations_test"

BOOKING_TABLES = {"master_settings", "schedule_templates", "schedule_exceptions", "bookings"}
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


async def index_names(engine: AsyncEngine, table: str) -> set[str]:
    async with engine.connect() as connection:
        indexes = await connection.run_sync(lambda sync: inspect(sync).get_indexes(table))
    return {str(index["name"]) for index in indexes}


async def test_upgrade_head_creates_the_whole_booking_schema(
    dsn: str, migrations_engine: AsyncEngine
) -> None:
    await upgrade(dsn)

    assert BOOKING_TABLES | SHARED_TABLES <= await table_names(migrations_engine)


async def test_every_index_of_bookings_is_present(dsn: str, migrations_engine: AsyncEngine) -> None:
    await upgrade(dsn)

    assert {
        "ix_bookings_master_id_start_at",
        "ix_bookings_client_user_id_start_at",
        "ix_bookings_salon_id_start_at",
        "ix_bookings_reminder_at",
    } <= await index_names(migrations_engine, "bookings")


async def check_names(engine: AsyncEngine, table: str) -> set[str]:
    async with engine.connect() as connection:
        checks = await connection.run_sync(lambda sync: inspect(sync).get_check_constraints(table))
    return {str(check["name"]) for check in checks}


async def column_names(engine: AsyncEngine, table: str) -> set[str]:
    async with engine.connect() as connection:
        columns = await connection.run_sync(lambda sync: inspect(sync).get_columns(table))
    return {str(column["name"]) for column in columns}


async def test_the_cancel_deadline_is_kept_with_the_booking(
    dsn: str, migrations_engine: AsyncEngine
) -> None:
    await upgrade(dsn)

    assert "cancel_deadline_min" in await column_names(migrations_engine, "bookings")
    assert "ck_bookings_cancel_deadline_min_not_negative" in await check_names(
        migrations_engine, "bookings"
    )


async def test_the_cancel_deadline_downgrades_away(
    dsn: str, migrations_engine: AsyncEngine
) -> None:
    await upgrade(dsn)

    await downgrade(dsn, "0003_idempotency_keys")

    assert "cancel_deadline_min" not in await column_names(migrations_engine, "bookings")


async def test_downgrade_removes_the_booking_tables(
    dsn: str, migrations_engine: AsyncEngine
) -> None:
    await upgrade(dsn)

    await downgrade(dsn, "0001_shared_tables")

    assert BOOKING_TABLES & await table_names(migrations_engine) == set()


async def test_the_schema_can_be_applied_again_after_a_full_downgrade(
    dsn: str, migrations_engine: AsyncEngine
) -> None:
    await upgrade(dsn)
    await downgrade(dsn)

    await upgrade(dsn)

    assert BOOKING_TABLES <= await table_names(migrations_engine)
