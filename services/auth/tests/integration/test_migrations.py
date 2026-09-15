"""The auth schema, applied and removed by the real migration scripts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from alembic import command
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine

from barber_auth.settings import ALEMBIC_INI
from barber_common.db import create_engine
from barber_common.db.migrations import load_config
from barber_common.testing.fixtures import create_database

pytestmark = pytest.mark.integration

DATABASE_NAME = "auth_migrations_test"

AUTH_TABLES = {
    "users",
    "user_roles",
    "refresh_tokens",
    "email_confirmations",
    "signing_keys",
    "service_clients",
}
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


async def test_upgrade_head_creates_the_whole_auth_schema(
    dsn: str, migrations_engine: AsyncEngine
) -> None:
    await upgrade(dsn)

    assert AUTH_TABLES | SHARED_TABLES <= await table_names(migrations_engine)


async def test_uniqueness_of_the_address_is_an_index_over_lower_email(
    dsn: str, migrations_engine: AsyncEngine
) -> None:
    await upgrade(dsn)

    assert {"uq_users_lower_email", "uq_users_phone"} <= await index_names(
        migrations_engine, "users"
    )


async def test_a_role_is_unique_within_its_scope(dsn: str, migrations_engine: AsyncEngine) -> None:
    await upgrade(dsn)

    # Two partial indexes instead of a primary key, because a global role has a
    # null salon_id.
    assert {
        "uq_user_roles_user_id_role_global",
        "uq_user_roles_user_id_role_salon_id",
    } <= await index_names(migrations_engine, "user_roles")


async def test_downgrade_base_removes_everything_it_created(
    dsn: str, migrations_engine: AsyncEngine
) -> None:
    await upgrade(dsn)

    await downgrade(dsn)

    assert (AUTH_TABLES | SHARED_TABLES) & await table_names(migrations_engine) == set()


async def test_the_schema_can_be_applied_again_after_a_full_downgrade(
    dsn: str, migrations_engine: AsyncEngine
) -> None:
    await upgrade(dsn)
    await downgrade(dsn)

    await upgrade(dsn)

    # A downgrade that leaves an index behind makes the next upgrade fail; this
    # is the assertion that catches it.
    assert AUTH_TABLES <= await table_names(migrations_engine)
