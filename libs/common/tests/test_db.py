"""Sessions and transactions against a real PostgreSQL from testcontainers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import QueuePool

from barber_common.db import Database, create_engine, transaction, unit_of_work

pytestmark = pytest.mark.integration


class WorkFailed(RuntimeError):
    """Raised inside a unit of work to check that the transaction rolls back."""


@pytest.fixture
async def engine(postgres_dsn: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(postgres_dsn, pool_size=5, max_overflow=0)
    # Plain DDL rather than metadata.create_all: service schemas are built by
    # Alembic, and this table exists only to exercise the session layer.
    async with engine.begin() as connection:
        await connection.execute(text("DROP TABLE IF EXISTS probes"))
        await connection.execute(text("CREATE TABLE probes (name text PRIMARY KEY)"))

    yield engine

    await engine.dispose()


async def count_probes(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        result = await connection.execute(text("SELECT count(*) FROM probes"))
        return int(result.scalar_one())


async def insert_probe(session_factory_engine: AsyncEngine, name: str) -> None:
    database = Database(session_factory_engine)
    async with database.unit_of_work() as session:
        await session.execute(text("INSERT INTO probes VALUES (:name)"), {"name": name})


async def test_unit_of_work_commits_on_a_clean_exit(engine: AsyncEngine) -> None:
    await insert_probe(engine, "committed")

    assert await count_probes(engine) == 1


async def test_unit_of_work_rolls_back_on_an_exception(engine: AsyncEngine) -> None:
    database = Database(engine)

    with pytest.raises(WorkFailed):
        async with database.unit_of_work() as session:
            await session.execute(text("INSERT INTO probes VALUES ('doomed')"))
            raise WorkFailed

    assert await count_probes(engine) == 0


async def test_nested_transaction_joins_the_open_one(engine: AsyncEngine) -> None:
    database = Database(engine)

    async with database.unit_of_work() as session:
        outer = session.get_transaction()
        async with transaction(session):
            inner = session.get_transaction()

        assert inner is outer
        assert session.in_transaction()  # the inner block did not commit


async def test_nested_transaction_rolls_back_together_with_the_outer_one(
    engine: AsyncEngine,
) -> None:
    database = Database(engine)

    with pytest.raises(WorkFailed):
        async with database.unit_of_work() as session:
            async with transaction(session):
                await session.execute(text("INSERT INTO probes VALUES ('inner')"))
            raise WorkFailed

    assert await count_probes(engine) == 0


async def test_concurrent_units_of_work_return_every_connection_to_the_pool(
    engine: AsyncEngine,
) -> None:
    async def work(number: int) -> None:
        async with unit_of_work(Database(engine).session_factory) as session:
            await session.execute(
                text("INSERT INTO probes VALUES (:name)"), {"name": f"probe-{number}"}
            )

    await asyncio.gather(*[work(number) for number in range(20)])

    pool = engine.pool
    assert isinstance(pool, QueuePool)
    assert await count_probes(engine) == 20
    assert pool.checkedout() == 0


async def test_check_connection_succeeds_against_a_live_database(
    engine: AsyncEngine,
) -> None:
    database = Database(engine)

    await database.check_connection()


async def test_check_connection_fails_when_the_database_is_unreachable() -> None:
    engine = create_engine("postgresql+asyncpg://nobody:nobody@127.0.0.1:1/nothing")
    database = Database(engine)

    with pytest.raises(OSError):
        await database.check_connection()

    await engine.dispose()
