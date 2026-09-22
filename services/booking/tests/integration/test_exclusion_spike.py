"""Whether PostgreSQL can hold the main invariant the way the schema intends.

Runs raw DDL against a database of its own, before any model or migration
exists, and answers three questions: can ``occupied_range`` be a generated
column, does a partial ``EXCLUDE USING gist`` over it get created, and do two
concurrent overlapping inserts really end with one ``23P01``.

The first answer is no: ``timestamptz + interval`` is ``STABLE``, because an
interval of days depends on the time zone, and a generated column demands an
``IMMUTABLE`` expression. The range is therefore an ordinary column written by
the application, and a ``CHECK`` with the same expression -- a check may be
``STABLE`` -- refuses any row whose range disagrees with its times.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from barber_common.db import create_engine
from barber_common.db.errors import (
    SQLSTATE_CHECK_VIOLATION,
    SQLSTATE_EXCLUSION_VIOLATION,
    sqlstate_of,
)
from barber_common.testing.fixtures import create_database

pytestmark = pytest.mark.integration

DATABASE_NAME = "booking_exclusion_spike"

# invalid_object_definition: what PostgreSQL answers to a generation
# expression that is not immutable.
SQLSTATE_INVALID_OBJECT_DEFINITION = "42P17"

GENERATED_COLUMN_DDL = """
CREATE TABLE spike_generated (
    start_at       timestamptz NOT NULL,
    end_at         timestamptz NOT NULL,
    buffer_min     int         NOT NULL,
    occupied_range tstzrange GENERATED ALWAYS AS (
        tstzrange(start_at, end_at + make_interval(mins => buffer_min), '[)')
    ) STORED
)
"""

CHOSEN_DDL = """
CREATE TABLE spike_bookings (
    id             uuid        PRIMARY KEY,
    master_id      uuid        NOT NULL,
    start_at       timestamptz NOT NULL,
    end_at         timestamptz NOT NULL,
    buffer_min     int         NOT NULL,
    status         text        NOT NULL,
    occupied_range tstzrange   NOT NULL,
    CONSTRAINT occupied_range_matches_times CHECK (
        occupied_range = tstzrange(start_at, end_at + make_interval(mins => buffer_min), '[)')
    ),
    CONSTRAINT no_overlapping_bookings EXCLUDE USING gist (
        master_id WITH =,
        occupied_range WITH &&
    ) WHERE (status IN ('pending', 'confirmed', 'completed', 'no_show'))
)
"""

INSERT = text(
    """
    INSERT INTO spike_bookings
        (id, master_id, start_at, end_at, buffer_min, status, occupied_range)
    VALUES
        (:id, :master_id, :start_at, :end_at, :buffer_min, :status,
         tstzrange(
             CAST(:start_at AS timestamptz),
             CAST(:end_at AS timestamptz) + make_interval(mins => CAST(:buffer_min AS int)),
             '[)'
         ))
    """
)

TEN_O_CLOCK = datetime(2026, 10, 5, 7, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def spike_dsn(postgres_dsn: str) -> str:
    return create_database(postgres_dsn, DATABASE_NAME)


@pytest.fixture
async def engine(spike_dsn: str) -> AsyncIterator[AsyncEngine]:
    """An engine over a freshly laid out table.

    Real connections rather than savepoints on one: the race below needs two
    transactions that genuinely run side by side.
    """
    engine = create_engine(spike_dsn, pool_size=4, max_overflow=0)
    async with engine.begin() as connection:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
        await connection.execute(text("DROP TABLE IF EXISTS spike_bookings"))
        await connection.execute(text(CHOSEN_DDL))

    yield engine

    await engine.dispose()


def booking(
    master_id: UUID,
    start_at: datetime,
    *,
    duration_min: int = 45,
    buffer_min: int = 0,
    status: str = "confirmed",
) -> dict[str, object]:
    return {
        "id": uuid4(),
        "master_id": master_id,
        "start_at": start_at,
        "end_at": start_at + timedelta(minutes=duration_min),
        "buffer_min": buffer_min,
        "status": status,
    }


async def wait_until_blocked(engine: AsyncEngine) -> None:
    """Return once some statement of this database waits on a lock."""
    query = text(
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE datname = current_database() AND wait_event_type = 'Lock'"
    )
    async with asyncio.timeout(5.0), engine.connect() as observer:
        # Polling, not an Event: the wait happens in another backend, and the
        # only way to see it is to ask the server.
        while (await observer.execute(query)).scalar_one() == 0:  # noqa: ASYNC110
            await asyncio.sleep(0.02)


async def insert(connection: AsyncConnection, row: dict[str, object]) -> None:
    await connection.execute(INSERT, row)


async def test_a_generated_occupied_range_is_refused_as_not_immutable(
    engine: AsyncEngine,
) -> None:
    async with engine.connect() as connection:
        with pytest.raises(DBAPIError) as failure:
            await connection.execute(text(GENERATED_COLUMN_DDL))

    assert sqlstate_of(failure.value) == SQLSTATE_INVALID_OBJECT_DEFINITION
    assert "not immutable" in str(failure.value)


async def test_a_range_that_disagrees_with_the_times_is_refused(engine: AsyncEngine) -> None:
    row = booking(uuid4(), TEN_O_CLOCK, buffer_min=15)

    async with engine.connect() as connection:
        with pytest.raises(IntegrityError) as failure:
            # The buffer left out of the range: exactly the mistake a second
            # write path would make.
            await connection.execute(
                text(
                    "INSERT INTO spike_bookings "
                    "(id, master_id, start_at, end_at, buffer_min, status, occupied_range) "
                    "VALUES (:id, :master_id, :start_at, :end_at, :buffer_min, :status, "
                    "tstzrange(CAST(:start_at AS timestamptz), CAST(:end_at AS timestamptz), '[)'))"
                ),
                row,
            )

    assert sqlstate_of(failure.value) == SQLSTATE_CHECK_VIOLATION


async def test_of_two_concurrent_overlapping_inserts_the_second_gets_23p01(
    engine: AsyncEngine,
) -> None:
    master_id = uuid4()

    async with engine.connect() as first, engine.connect() as second:
        await first.begin()
        await insert(first, booking(master_id, TEN_O_CLOCK))

        await second.begin()
        # Blocks: the first row is not committed yet, so the constraint cannot
        # decide until the first transaction does.
        contender = asyncio.create_task(
            insert(second, booking(master_id, TEN_O_CLOCK + timedelta(minutes=30)))
        )
        await wait_until_blocked(engine)
        await first.commit()

        with pytest.raises(IntegrityError) as failure:
            await contender
        await second.rollback()

    assert sqlstate_of(failure.value) == SQLSTATE_EXCLUSION_VIOLATION


async def test_the_buffer_makes_the_next_start_overlap(engine: AsyncEngine) -> None:
    master_id = uuid4()

    async with engine.begin() as connection:
        await insert(connection, booking(master_id, TEN_O_CLOCK, buffer_min=15))

    async with engine.connect() as connection:
        with pytest.raises(IntegrityError) as failure:
            await insert(connection, booking(master_id, TEN_O_CLOCK + timedelta(minutes=45)))

    assert sqlstate_of(failure.value) == SQLSTATE_EXCLUSION_VIOLATION


async def test_a_cancelled_booking_frees_its_time(engine: AsyncEngine) -> None:
    master_id = uuid4()
    first = booking(master_id, TEN_O_CLOCK)

    async with engine.begin() as connection:
        await insert(connection, first)
        await connection.execute(
            text("UPDATE spike_bookings SET status = 'cancelled_by_client' WHERE id = :id"),
            {"id": first["id"]},
        )

    async with engine.begin() as connection:
        await insert(connection, booking(master_id, TEN_O_CLOCK))

    async with engine.connect() as connection:
        count = await connection.execute(text("SELECT count(*) FROM spike_bookings"))
    assert count.scalar_one() == 2
