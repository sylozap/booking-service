"""Self-check of the test infrastructure.

Fixtures that leak state between tests produce a suite that passes in one order
and fails in another, and the failure is blamed on the code under test for a
day or two before anyone suspects the fixtures. These tests are cheap insurance
against that.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from barber_common.app import create_app
from barber_common.config import BaseAppSettings
from barber_common.db import unit_of_work
from barber_common.outbox.models import OutboxMessage
from barber_common.testing.factories import envelope_factory, outbox_message_factory
from barber_common.testing.fixtures import app_client, wait_for

pytestmark = pytest.mark.integration


async def write_one_and_count(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """Write a row, then count what this transaction can see."""
    async with unit_of_work(session_factory) as session:
        session.add(outbox_message_factory())
        await session.flush()
        result = await session.execute(select(func.count()).select_from(OutboxMessage))
        return int(result.scalar_one())


async def test_a_test_sees_only_its_own_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    assert await write_one_and_count(session_factory) == 1


async def test_the_next_test_writing_the_same_table_sees_only_its_own_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Same table, same database, right after the test above. Two rows here
    # would mean the rollback of the previous test did not happen.
    assert await write_one_and_count(session_factory) == 1


async def test_a_commit_inside_the_test_is_still_rolled_back_afterwards(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with unit_of_work(session_factory) as session:
        session.add(outbox_message_factory())

    async with unit_of_work(session_factory) as session:
        result = await session.execute(select(func.count()).select_from(OutboxMessage))

    assert int(result.scalar_one()) == 1


async def test_the_application_client_reaches_the_probes(settings: BaseAppSettings) -> None:
    app = create_app(settings)

    async with app_client(app) as client:
        response = await client.get("/health/live")

    assert response.status_code == 200


async def test_wait_for_returns_as_soon_as_the_condition_holds() -> None:
    flag = {"ready": False}

    async def flip() -> None:
        await asyncio.sleep(0.05)
        flag["ready"] = True

    task = asyncio.create_task(flip())
    await wait_for(lambda: flag["ready"], timeout_seconds=5.0)
    await task

    assert flag["ready"]


async def test_wait_for_gives_up_at_the_deadline() -> None:
    with pytest.raises(TimeoutError):
        await wait_for(lambda: False, timeout_seconds=0.1, interval_seconds=0.01)


def test_the_envelope_factory_fills_in_what_the_test_did_not_name() -> None:
    envelope = envelope_factory(event_type="booking.created")

    assert envelope.event_type == "booking.created"
    assert envelope.producer
    assert envelope.occurred_at.tzinfo is not None
