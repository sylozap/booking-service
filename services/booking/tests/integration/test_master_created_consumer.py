"""Master settings follow catalog.masters.v1.

The handlers run through the real consumer runner, with the deduplication table
of the real database. Nothing but the Kafka client is replaced.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from aiokafka import ConsumerRecord
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from barber_booking.consumers.master_lifecycle import (
    MASTER_LIFECYCLE_GROUP,
    MASTER_LIFECYCLE_TOPICS,
    MasterLifecycle,
)
from barber_booking.models.master_settings import MasterSettings
from barber_common.events.catalog import (
    CATALOG_MASTERS_TOPIC,
    MasterCreated,
    MasterEventType,
    MasterUpdated,
)
from barber_common.events.envelope import build_envelope
from barber_common.kafka import EventConsumer, ProcessingResult, RetryPolicy

pytestmark = pytest.mark.integration

MasterSettingsFactory = Callable[..., Awaitable[MasterSettings]]


def record(event_type: str, payload: dict[str, object]) -> ConsumerRecord[bytes, bytes]:
    envelope = build_envelope(event_type=event_type, payload=payload, producer="catalog@test")
    value = envelope.model_dump_json().encode("utf-8")
    return ConsumerRecord(
        topic=CATALOG_MASTERS_TOPIC,
        partition=0,
        offset=0,
        timestamp=0,
        timestamp_type=0,
        key=None,
        value=value,
        checksum=None,
        serialized_key_size=0,
        serialized_value_size=len(value),
        headers=[],
    )


def created(
    master_id: UUID,
    *,
    salon_id: UUID | None = None,
    user_id: UUID | None = None,
    timezone: str = "Europe/Moscow",
) -> ConsumerRecord[bytes, bytes]:
    payload = MasterCreated(
        master_id=master_id,
        salon_id=salon_id or uuid4(),
        user_id=user_id or uuid4(),
        display_name="Ivan",
        timezone=timezone,
        is_active=True,
    )
    return record(MasterEventType.CREATED, payload.model_dump(mode="json"))


def updated(
    master_id: UUID, *, timezone: str | None, is_active: bool = True
) -> ConsumerRecord[bytes, bytes]:
    payload = MasterUpdated(
        master_id=master_id,
        salon_id=uuid4(),
        user_id=uuid4(),
        display_name="Ivan",
        specialization=None,
        is_active=is_active,
        timezone=timezone,
    )
    return record(MasterEventType.UPDATED, payload.model_dump(mode="json"))


@pytest.fixture
def consumer(session_factory: async_sessionmaker[AsyncSession]) -> EventConsumer:
    """The runner over a client that is never started: ``handle`` needs none."""
    return EventConsumer(
        topics=MASTER_LIFECYCLE_TOPICS,
        group_id=MASTER_LIFECYCLE_GROUP,
        session_factory=session_factory,
        dead_letters=AsyncMock(),
        handlers=MasterLifecycle().handlers(),
        retry_policy=RetryPolicy(attempts=1),
        client=MagicMock(),
    )


async def settings_of(session: AsyncSession, master_id: UUID) -> list[MasterSettings]:
    statement = (
        select(MasterSettings)
        .where(MasterSettings.master_id == master_id)
        .execution_options(populate_existing=True)
    )
    return list((await session.execute(statement)).scalars().all())


# --- master.created ---------------------------------------------------------


async def test_a_new_master_gets_settings_with_the_default_buffer(
    consumer: EventConsumer, session: AsyncSession
) -> None:
    master_id, salon_id, user_id = uuid4(), uuid4(), uuid4()

    result = await consumer.handle(
        created(master_id, salon_id=salon_id, user_id=user_id, timezone="Asia/Yekaterinburg")
    )

    assert result is ProcessingResult.OK
    [row] = await settings_of(session, master_id)
    assert (row.salon_id, row.user_id) == (salon_id, user_id)
    assert row.timezone == "Asia/Yekaterinburg"
    assert row.buffer_after_min == 0
    assert row.is_active is True


async def test_a_redelivered_creation_makes_no_second_row(
    consumer: EventConsumer, session: AsyncSession
) -> None:
    master_id = uuid4()
    message = created(master_id)

    first = await consumer.handle(message)
    second = await consumer.handle(message)

    assert (first, second) == (ProcessingResult.OK, ProcessingResult.DUPLICATE)
    assert len(await settings_of(session, master_id)) == 1


async def test_a_creation_arriving_after_the_lazy_path_keeps_what_is_there(
    consumer: EventConsumer,
    session: AsyncSession,
    make_master_settings: MasterSettingsFactory,
) -> None:
    master = await make_master_settings(buffer_after_min=15)

    result = await consumer.handle(created(master.master_id))

    assert result is ProcessingResult.OK
    [row] = await settings_of(session, master.master_id)
    assert row.buffer_after_min == 15


async def test_a_zone_that_does_not_exist_creates_nothing(
    consumer: EventConsumer, session: AsyncSession
) -> None:
    master_id = uuid4()

    result = await consumer.handle(created(master_id, timezone="Mars/Olympus_Mons"))

    assert result is ProcessingResult.DEAD_LETTERED
    assert await settings_of(session, master_id) == []


# --- master.updated ---------------------------------------------------------


async def test_a_new_zone_of_the_salon_reaches_the_settings(
    consumer: EventConsumer,
    session: AsyncSession,
    make_master_settings: MasterSettingsFactory,
) -> None:
    master = await make_master_settings(timezone="Europe/Moscow", buffer_after_min=10)

    await consumer.handle(updated(master.master_id, timezone="Asia/Yekaterinburg"))

    [row] = await settings_of(session, master.master_id)
    assert row.timezone == "Asia/Yekaterinburg"
    assert row.buffer_after_min == 10


async def test_a_reactivated_master_is_active_again(
    consumer: EventConsumer,
    session: AsyncSession,
    make_master_settings: MasterSettingsFactory,
) -> None:
    master = await make_master_settings(is_active=False)

    await consumer.handle(updated(master.master_id, timezone="Europe/Moscow", is_active=True))

    [row] = await settings_of(session, master.master_id)
    assert row.is_active is True


async def test_a_snapshot_without_a_zone_leaves_the_zone_alone(
    consumer: EventConsumer,
    session: AsyncSession,
    make_master_settings: MasterSettingsFactory,
) -> None:
    master = await make_master_settings(timezone="Asia/Yekaterinburg")

    await consumer.handle(updated(master.master_id, timezone=None))

    [row] = await settings_of(session, master.master_id)
    assert row.timezone == "Asia/Yekaterinburg"


async def test_a_master_never_seen_gets_settings_from_the_snapshot(
    consumer: EventConsumer, session: AsyncSession
) -> None:
    master_id = uuid4()

    await consumer.handle(updated(master_id, timezone="Asia/Yekaterinburg"))

    [row] = await settings_of(session, master_id)
    assert row.timezone == "Asia/Yekaterinburg"


async def test_a_snapshot_without_a_zone_cannot_create_settings(
    consumer: EventConsumer, session: AsyncSession
) -> None:
    master_id = uuid4()

    result = await consumer.handle(updated(master_id, timezone=None))

    assert result is ProcessingResult.OK
    assert await settings_of(session, master_id) == []
