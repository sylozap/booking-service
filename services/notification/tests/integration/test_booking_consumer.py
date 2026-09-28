"""Booking events become queued messages, once each.

The handlers run through the real consumer runner, with the deduplication table
of the real database. Nothing but the Kafka client is replaced.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from aiokafka import ConsumerRecord
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from barber_common.events.bookings import (
    BOOKINGS_TOPIC,
    BookingCancelled,
    BookingCompleted,
    BookingCreated,
    BookingEventType,
    BookingRescheduled,
    CancelledBy,
)
from barber_common.events.envelope import build_envelope
from barber_common.kafka import EventConsumer, ProcessingResult, RetryPolicy
from barber_notification.consumers.booking_events import (
    BOOKING_EVENTS_GROUP,
    BOOKING_EVENTS_TOPICS,
    BookingEvents,
)
from barber_notification.models.notification import Notification
from barber_notification.models.recipient import Recipient
from barber_notification.services.dispatch import EnqueueNotification

pytestmark = pytest.mark.integration

RecipientFactory = Callable[..., Awaitable[Recipient]]

START_AT = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def record(event_type: str, payload: BaseModel) -> ConsumerRecord[bytes, bytes]:
    envelope = build_envelope(
        event_type=event_type, payload=payload.model_dump(mode="json"), producer="booking@test"
    )
    value = envelope.model_dump_json().encode("utf-8")
    return ConsumerRecord(
        topic=BOOKINGS_TOPIC,
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


def created(client_user_id: UUID) -> ConsumerRecord[bytes, bytes]:
    return record(
        BookingEventType.CREATED,
        BookingCreated(
            booking_id=uuid4(),
            salon_id=uuid4(),
            master_id=uuid4(),
            client_user_id=client_user_id,
            service_id=uuid4(),
            service_name="Стрижка",
            price="3500.00",
            currency="RUB",
            start_at=START_AT,
            end_at=START_AT + timedelta(minutes=45),
            status="confirmed",
            timezone="Europe/Moscow",
        ),
    )


def rescheduled(client_user_id: UUID) -> ConsumerRecord[bytes, bytes]:
    return record(
        BookingEventType.RESCHEDULED,
        BookingRescheduled(
            booking_id=uuid4(),
            salon_id=uuid4(),
            master_id=uuid4(),
            client_user_id=client_user_id,
            service_name="Стрижка",
            previous_start_at=START_AT,
            previous_end_at=START_AT + timedelta(minutes=45),
            start_at=START_AT + timedelta(days=1),
            end_at=START_AT + timedelta(days=1, minutes=45),
        ),
    )


def cancelled(client_user_id: UUID, by: CancelledBy) -> ConsumerRecord[bytes, bytes]:
    return record(
        BookingEventType.CANCELLED,
        BookingCancelled(
            booking_id=uuid4(),
            salon_id=uuid4(),
            master_id=uuid4(),
            client_user_id=client_user_id,
            service_name="Стрижка",
            start_at=START_AT,
            end_at=START_AT + timedelta(minutes=45),
            cancelled_by=by,
        ),
    )


@pytest.fixture
def consumer(session_factory: async_sessionmaker[AsyncSession]) -> EventConsumer:
    """The runner over a client that is never started: ``handle`` needs none."""
    return EventConsumer(
        topics=BOOKING_EVENTS_TOPICS,
        group_id=BOOKING_EVENTS_GROUP,
        session_factory=session_factory,
        dead_letters=AsyncMock(),
        handlers=BookingEvents().handlers(),
        retry_policy=RetryPolicy(attempts=1),
        client=MagicMock(),
    )


async def queued_for(session: AsyncSession, user_id: UUID) -> list[Notification]:
    statement = (
        select(Notification)
        .where(Notification.user_id == user_id)
        .order_by(Notification.channel)
        .execution_options(populate_existing=True)
    )
    return list((await session.execute(statement)).scalars().all())


async def test_a_new_booking_is_queued_on_every_channel_of_the_client(
    consumer: EventConsumer, session: AsyncSession, make_recipient: RecipientFactory
) -> None:
    client = await make_recipient(telegram_chat_id=42)

    result = await consumer.handle(created(client.user_id))

    assert result is ProcessingResult.OK
    rows = await queued_for(session, client.user_id)
    assert [(row.channel, row.template, row.status) for row in rows] == [
        ("email", "booking_created", "pending"),
        ("telegram", "booking_created", "pending"),
    ]
    assert rows[0].payload["start_at"] == "05.09.2026 15:00 (Europe/Moscow)"
    assert rows[0].dedup_key == f"{rows[0].event_id}:email"


@pytest.mark.parametrize(
    ("message", "template"),
    [
        (rescheduled, "booking_rescheduled"),
        (lambda user: cancelled(user, CancelledBy.CLIENT), "booking_cancelled_by_client"),
        (lambda user: cancelled(user, CancelledBy.SALON), "booking_cancelled_by_salon"),
    ],
)
async def test_each_change_of_a_booking_is_one_message(
    consumer: EventConsumer,
    session: AsyncSession,
    make_recipient: RecipientFactory,
    message: Callable[[UUID], ConsumerRecord[bytes, bytes]],
    template: str,
) -> None:
    client = await make_recipient()

    await consumer.handle(message(client.user_id))

    [row] = await queued_for(session, client.user_id)
    assert (row.channel, row.template) == ("email", template)


async def test_a_redelivered_event_queues_nothing_more(
    consumer: EventConsumer, session: AsyncSession, make_recipient: RecipientFactory
) -> None:
    client = await make_recipient(telegram_chat_id=42)
    message = created(client.user_id)

    first = await consumer.handle(message)
    second = await consumer.handle(message)

    assert (first, second) == (ProcessingResult.OK, ProcessingResult.DUPLICATE)
    assert len(await queued_for(session, client.user_id)) == 2


async def test_the_dedup_key_holds_even_past_the_processed_events(
    session: AsyncSession, make_recipient: RecipientFactory
) -> None:
    # Two consumer groups, or a processed_events row pruned too early: the
    # journal itself still refuses a second message for the same event.
    client = await make_recipient()
    event_id = uuid4()
    enqueue = EnqueueNotification(session)
    fields: dict[str, object] = {"service_name": "Стрижка", "start_at": "05.09.2026 15:00"}

    await enqueue.execute(
        event_id=event_id, user_id=client.user_id, template="booking_created", fields=fields
    )
    await enqueue.execute(
        event_id=event_id, user_id=client.user_id, template="booking_created", fields=fields
    )

    assert len(await queued_for(session, client.user_id)) == 1


async def test_a_client_this_service_has_not_heard_of_is_logged_and_skipped(
    consumer: EventConsumer, session: AsyncSession
) -> None:
    unknown = uuid4()

    result = await consumer.handle(created(unknown))

    assert result is ProcessingResult.OK
    assert await queued_for(session, unknown) == []


async def test_a_channel_the_client_switched_off_is_not_used(
    consumer: EventConsumer, session: AsyncSession, make_recipient: RecipientFactory
) -> None:
    client = await make_recipient(
        telegram_chat_id=42, preferences={"channels": {"telegram": False}}
    )

    await consumer.handle(created(client.user_id))

    assert [row.channel for row in await queued_for(session, client.user_id)] == ["email"]


async def test_a_deactivated_client_is_told_nothing(
    consumer: EventConsumer, session: AsyncSession, make_recipient: RecipientFactory
) -> None:
    client = await make_recipient(is_active=False)

    await consumer.handle(created(client.user_id))

    assert await queued_for(session, client.user_id) == []


async def test_a_closed_visit_is_no_message(
    consumer: EventConsumer, session: AsyncSession, make_recipient: RecipientFactory
) -> None:
    client = await make_recipient()
    closed = BookingCompleted(
        booking_id=uuid4(),
        salon_id=uuid4(),
        master_id=uuid4(),
        client_user_id=client.user_id,
        service_name="Стрижка",
        start_at=START_AT,
        end_at=START_AT + timedelta(minutes=45),
    )

    result = await consumer.handle(record(BookingEventType.COMPLETED, closed))

    assert result is ProcessingResult.SKIPPED
    assert await queued_for(session, client.user_id) == []
