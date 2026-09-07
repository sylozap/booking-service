"""Reading the SQLSTATE and the constraint out of a real database failure.

Against PostgreSQL rather than a fabricated exception: what is being checked
here is exactly where the asyncpg dialect puts the two values, and a hand built
error would only prove that the helper reads the attribute the test set.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from barber_common.db.errors import (
    SQLSTATE_UNIQUE_VIOLATION,
    constraint_name_of,
    is_unique_violation,
    sqlstate_of,
)
from barber_common.kafka.dedup import ProcessedEvent

pytestmark = pytest.mark.integration

GROUP = "test-group"


async def insert_twice(session: AsyncSession) -> IntegrityError:
    """Write the same deduplication row twice and return what came back."""
    event_id = uuid4()
    session.add(ProcessedEvent(event_id=event_id, consumer_group=GROUP))
    await session.flush()

    session.add(ProcessedEvent(event_id=event_id, consumer_group=GROUP))
    with pytest.raises(IntegrityError) as failure:
        await session.flush()
    return failure.value


async def test_a_duplicate_row_reports_the_unique_violation_sqlstate(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        error = await insert_twice(session)

    assert sqlstate_of(error) == SQLSTATE_UNIQUE_VIOLATION


async def test_the_constraint_that_failed_is_named(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        error = await insert_twice(session)

    # The name is what lets a caller tell two unique indexes of one table
    # apart; the SQLSTATE alone says only "something is not unique".
    assert constraint_name_of(error) == "pk_processed_events"


async def test_a_violation_is_recognised_by_its_constraint(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        error = await insert_twice(session)

    assert is_unique_violation(error) is True
    assert is_unique_violation(error, constraint="pk_processed_events") is True
    assert is_unique_violation(error, constraint="uq_something_else") is False


async def test_another_kind_of_failure_is_not_a_unique_violation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        with pytest.raises(ProgrammingError) as failure:
            await session.execute(text("SELECT * FROM a_table_that_does_not_exist"))

    # Catching IntegrityError as a whole is what this rules out: a broken
    # statement must not be reported as a duplicate.
    assert is_unique_violation(failure.value) is False
    assert sqlstate_of(failure.value) == "42P01"
