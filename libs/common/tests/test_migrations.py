"""The shared revision and the schema version check, against a real database."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine

from barber_common.db import create_engine
from barber_common.db.migrations import (
    SchemaVersionMismatch,
    check_schema_is_current,
    current_revisions,
    head_revisions,
    load_config,
)
from barber_common.testing.fixtures import create_database

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).parent / "alembic.ini"
DATABASE_NAME = "migrations_test"


@pytest.fixture
def dsn(postgres_dsn: str) -> str:
    """A database of its own, so a downgrade here breaks no other test."""
    return create_database(postgres_dsn, DATABASE_NAME)


@pytest.fixture
async def engine(dsn: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(dsn, pool_size=2, max_overflow=0)

    yield engine

    await engine.dispose()


async def upgrade(dsn: str, revision: str = "head") -> None:
    # Alembic runs its own event loop, so it cannot run inside this one.
    await asyncio.to_thread(command.upgrade, load_config(ALEMBIC_INI, dsn=dsn), revision)


async def downgrade(dsn: str, revision: str = "base") -> None:
    await asyncio.to_thread(command.downgrade, load_config(ALEMBIC_INI, dsn=dsn), revision)


async def table_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as connection:
        return set(await connection.run_sync(lambda sync: inspect(sync).get_table_names()))


async def test_upgrade_head_creates_the_shared_tables(dsn: str, engine: AsyncEngine) -> None:
    await upgrade(dsn)

    assert {"outbox", "processed_events"} <= await table_names(engine)


async def test_the_partial_index_of_the_outbox_is_created(dsn: str, engine: AsyncEngine) -> None:
    await upgrade(dsn)

    async with engine.connect() as connection:
        indexes = await connection.run_sync(lambda sync: inspect(sync).get_indexes("outbox"))

    assert [index["name"] for index in indexes] == ["ix_outbox_created_at"]


async def test_downgrade_base_removes_everything_it_created(dsn: str, engine: AsyncEngine) -> None:
    await upgrade(dsn)

    await downgrade(dsn)

    assert {"outbox", "processed_events"} & await table_names(engine) == set()


async def test_the_check_passes_when_the_database_is_at_head(dsn: str, engine: AsyncEngine) -> None:
    await upgrade(dsn)

    await check_schema_is_current(engine, load_config(ALEMBIC_INI, dsn=dsn))


async def test_the_check_fails_when_the_database_is_behind(dsn: str, engine: AsyncEngine) -> None:
    config = load_config(ALEMBIC_INI, dsn=dsn)

    with pytest.raises(SchemaVersionMismatch) as failure:
        await check_schema_is_current(engine, config)

    # The message has to name both sides: an operator reading it at three in
    # the morning should not have to run alembic to learn what is missing.
    assert str(sorted(head_revisions(config))) in str(failure.value)
    assert "alembic upgrade head" in str(failure.value)


async def test_the_database_reports_the_revision_it_was_upgraded_to(
    dsn: str, engine: AsyncEngine
) -> None:
    await upgrade(dsn)

    assert await current_revisions(engine) == head_revisions(load_config(ALEMBIC_INI, dsn=dsn))
