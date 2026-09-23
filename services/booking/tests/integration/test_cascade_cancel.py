"""master.deactivated cancels the master's future bookings.

Through the real consumer runner and the real database: the cascade, the
events it queues and the row marking the event processed are one transaction.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from aiokafka import ConsumerRecord
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from barber_booking.consumers.master_lifecycle import (
    MASTER_LIFECYCLE_GROUP,
    MASTER_LIFECYCLE_TOPICS,
    MasterLifecycle,
)
from barber_booking.models.booking import Booking
from barber_booking.models.master_settings import MasterSettings
from barber_common.events.bookings import BookingEventType
from barber_common.events.catalog import (
    CATALOG_MASTERS_TOPIC,
    MasterDeactivated,
    MasterEventType,
)
from barber_common.events.envelope import build_envelope
from barber_common.kafka import EventConsumer, ProcessingResult, RetryPolicy
from barber_common.outbox.models import OutboxMessage

pytestmark = pytest.mark.integration

MasterSettingsFactory = Callable[..., Awaitable[MasterSettings]]
BookingFactory = Callable[..., Awaitable[Booking]]


def deactivated(master_id: UUID, salon_id: UUID) -> tuple[ConsumerRecord[bytes, bytes], UUID]:
    """The event and its ``event_id``, which every cancellation names as its cause."""
    payload = MasterDeactivated(master_id=master_id, salon_id=salon_id)
    envelope = build_envelope(
        event_type=MasterEventType.DEACTIVATED,
        payload=payload.model_dump(mode="json"),
        producer="catalog@test",
    )
    value = envelope.model_dump_json().encode("utf-8")
    message = ConsumerRecord(
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
    return message, envelope.event_id


@pytest.fixture
def consumer(session_factory: async_sessionmaker[AsyncSession]) -> EventConsumer:
    return EventConsumer(
        topics=MASTER_LIFECYCLE_TOPICS,
        group_id=MASTER_LIFECYCLE_GROUP,
        session_factory=session_factory,
        dead_letters=AsyncMock(),
        handlers=MasterLifecycle().handlers(),
        retry_policy=RetryPolicy(attempts=1),
        client=MagicMock(),
    )


@pytest.fixture
async def master(make_master_settings: MasterSettingsFactory) -> AsyncIterator[MasterSettings]:
    yield await make_master_settings()


def hours_from_now(hours: float) -> datetime:
    return (datetime.now(UTC) + timedelta(hours=hours)).replace(microsecond=0)


async def statuses(session: AsyncSession, *bookings: Booking) -> list[str]:
    statement = (
        select(Booking.id, Booking.status)
        .where(Booking.id.in_([booking.id for booking in bookings]))
        .execution_options(populate_existing=True)
    )
    found = dict((await session.execute(statement)).tuples().all())
    return [found[booking.id] for booking in bookings]


async def cancellations(session: AsyncSession) -> list[OutboxMessage]:
    statement = select(OutboxMessage).where(
        OutboxMessage.event_type == BookingEventType.CANCELLED.value
    )
    return list((await session.execute(statement)).scalars().all())


@contextmanager
def counting_statements(engine: AsyncEngine) -> Iterator[list[str]]:
    """Every statement sent to the database while the block runs."""
    sent: list[str] = []

    def record(*args: object) -> None:
        sent.append(str(args[2]))

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield sent
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


# --- what is cancelled ------------------------------------------------------


async def test_every_future_visit_of_the_master_is_cancelled_by_the_salon(
    consumer: EventConsumer,
    session: AsyncSession,
    master: MasterSettings,
    make_booking: BookingFactory,
) -> None:
    tomorrow = await make_booking(master_id=master.master_id, start_at=hours_from_now(24))
    next_week = await make_booking(
        master_id=master.master_id, start_at=hours_from_now(24 * 7), status="pending"
    )
    message, _ = deactivated(master.master_id, master.salon_id)

    result = await consumer.handle(message)

    assert result is ProcessingResult.OK
    assert await statuses(session, tomorrow, next_week) == ["cancelled_by_salon"] * 2
    row = await session.get(Booking, tomorrow.id, populate_existing=True)
    assert row is not None
    assert row.cancelled_by is None
    assert row.cancel_reason == "master_deactivated"


async def test_the_past_and_what_is_under_way_are_left_alone(
    consumer: EventConsumer,
    session: AsyncSession,
    master: MasterSettings,
    make_booking: BookingFactory,
) -> None:
    yesterday = await make_booking(master_id=master.master_id, start_at=hours_from_now(-24))
    under_way = await make_booking(master_id=master.master_id, start_at=hours_from_now(-0.25))
    completed = await make_booking(
        master_id=master.master_id, start_at=hours_from_now(-48), status="completed"
    )
    message, _ = deactivated(master.master_id, master.salon_id)

    await consumer.handle(message)

    assert await statuses(session, yesterday, under_way, completed) == [
        "confirmed",
        "confirmed",
        "completed",
    ]


async def test_a_booking_already_cancelled_keeps_who_cancelled_it(
    consumer: EventConsumer,
    session: AsyncSession,
    master: MasterSettings,
    make_booking: BookingFactory,
) -> None:
    by_client = await make_booking(
        master_id=master.master_id, start_at=hours_from_now(24), status="cancelled_by_client"
    )
    message, _ = deactivated(master.master_id, master.salon_id)

    await consumer.handle(message)

    assert await statuses(session, by_client) == ["cancelled_by_client"]
    assert await cancellations(session) == []


async def test_other_masters_keep_their_bookings(
    consumer: EventConsumer,
    session: AsyncSession,
    master: MasterSettings,
    make_master_settings: MasterSettingsFactory,
    make_booking: BookingFactory,
) -> None:
    colleague = await make_master_settings(salon_id=master.salon_id)
    theirs = await make_booking(master_id=colleague.master_id, start_at=hours_from_now(24))
    message, _ = deactivated(master.master_id, master.salon_id)

    await consumer.handle(message)

    assert await statuses(session, theirs) == ["confirmed"]


async def test_the_master_is_inactive_in_booking_afterwards(
    consumer: EventConsumer, session: AsyncSession, master: MasterSettings
) -> None:
    message, _ = deactivated(master.master_id, master.salon_id)

    await consumer.handle(message)

    row = await session.get(MasterSettings, master.master_id, populate_existing=True)
    assert row is not None
    assert row.is_active is False


async def test_a_master_booking_has_no_settings_for_still_loses_their_bookings(
    consumer: EventConsumer, session: AsyncSession, make_booking: BookingFactory
) -> None:
    master_id = uuid4()
    booking = await make_booking(master_id=master_id, start_at=hours_from_now(24))
    message, _ = deactivated(master_id, uuid4())

    result = await consumer.handle(message)

    assert result is ProcessingResult.OK
    assert await statuses(session, booking) == ["cancelled_by_salon"]


# --- the events -------------------------------------------------------------


async def test_each_cancellation_is_announced_once_and_names_its_cause(
    consumer: EventConsumer,
    session: AsyncSession,
    master: MasterSettings,
    make_booking: BookingFactory,
) -> None:
    bookings = [
        await make_booking(master_id=master.master_id, start_at=hours_from_now(24 * day))
        for day in (1, 2, 3)
    ]
    message, cause = deactivated(master.master_id, master.salon_id)

    await consumer.handle(message)

    events = await cancellations(session)
    assert sorted(event.aggregate_id for event in events) == sorted(b.id for b in bookings)
    assert {event.causation_id for event in events} == {cause}
    assert {event.payload["cancelled_by"] for event in events} == {"salon"}
    assert {event.payload["reason"] for event in events} == {"master_deactivated"}


async def test_a_redelivered_deactivation_changes_nothing_and_announces_nothing(
    consumer: EventConsumer,
    session: AsyncSession,
    master: MasterSettings,
    make_booking: BookingFactory,
) -> None:
    await make_booking(master_id=master.master_id, start_at=hours_from_now(24))
    message, _ = deactivated(master.master_id, master.salon_id)

    first = await consumer.handle(message)
    second = await consumer.handle(message)

    assert (first, second) == (ProcessingResult.OK, ProcessingResult.DUPLICATE)
    assert len(await cancellations(session)) == 1


# --- the size of the transaction --------------------------------------------


async def statements_to_cancel(
    count: int,
    *,
    consumer: EventConsumer,
    engine: AsyncEngine,
    make_master_settings: MasterSettingsFactory,
    make_booking: BookingFactory,
) -> int:
    master = await make_master_settings()
    for hour in range(count):
        await make_booking(master_id=master.master_id, start_at=hours_from_now(24 + hour))
    message, _ = deactivated(master.master_id, master.salon_id)

    with counting_statements(engine) as sent:
        result = await consumer.handle(message)

    assert result is ProcessingResult.OK
    return len(sent)


async def test_the_cascade_costs_the_same_statements_for_many_bookings_as_for_few(
    consumer: EventConsumer,
    engine: AsyncEngine,
    session: AsyncSession,
    make_master_settings: MasterSettingsFactory,
    make_booking: BookingFactory,
) -> None:
    few = await statements_to_cancel(
        3,
        consumer=consumer,
        engine=engine,
        make_master_settings=make_master_settings,
        make_booking=make_booking,
    )
    many = await statements_to_cancel(
        300,
        consumer=consumer,
        engine=engine,
        make_master_settings=make_master_settings,
        make_booking=make_booking,
    )

    assert many == few
    assert len(await cancellations(session)) == 303
