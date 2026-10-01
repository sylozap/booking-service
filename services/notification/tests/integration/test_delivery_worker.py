"""The delivery worker against a real database and a provider that records.

The provider is the only stand-in: it is the edge where Telegram would be.
The journal, the leases and the row locks are real.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from aiokafka import ConsumerRecord
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from barber_common.events.bookings import BOOKINGS_TOPIC, BookingCreated, BookingEventType
from barber_common.events.envelope import build_envelope
from barber_common.events.users import AUTH_USERS_TOPIC
from barber_common.kafka import EventConsumer, RetryPolicy
from barber_notification.consumers.booking_events import (
    BOOKING_EVENTS_GROUP,
    BOOKING_EVENTS_TOPICS,
    BookingEvents,
)
from barber_notification.models.notification import Notification
from barber_notification.models.recipient import Recipient
from barber_notification.preferences import NotificationKind
from barber_notification.providers.base import Channel, DeliveryResult, Message
from barber_notification.repositories.recipients import RecipientRepository
from barber_notification.services.dispatch import EnqueueNotification
from barber_notification.workers.delivery import DeliveryWorker

pytestmark = pytest.mark.integration

RecipientFactory = Callable[..., Awaitable[Recipient]]

FIELDS: dict[str, object] = {
    "service_name": "Стрижка",
    "start_at": "05.09.2026 15:00 (Europe/Moscow)",
}


class RecordingProvider(Protocol):
    """The provider of conftest: what it was given, and what it answers next."""

    sent: list[tuple[str, Message]]
    answers: list[DeliveryResult]


class Clock(Protocol):
    """The clock of conftest, moved by hand."""

    now: datetime


WorkerFactory = Callable[..., DeliveryWorker]


async def queue(
    session: AsyncSession,
    user_id: UUID,
    *,
    template: str = "booking_created",
    fields: dict[str, object] | None = None,
) -> UUID:
    event_id = uuid4()
    await EnqueueNotification(session).execute(
        event_id=event_id,
        topic=BOOKINGS_TOPIC,
        user_id=user_id,
        kind=NotificationKind.BOOKINGS,
        template=template,
        fields=fields or FIELDS,
    )
    return event_id


async def rows_of(session: AsyncSession, user_id: UUID) -> list[Notification]:
    statement = (
        select(Notification)
        .where(Notification.user_id == user_id)
        .order_by(Notification.channel)
        .execution_options(populate_existing=True)
    )
    return list((await session.execute(statement)).scalars().all())


async def test_a_queued_message_is_sent_and_recorded(
    worker: DeliveryWorker,
    email: RecordingProvider,
    session: AsyncSession,
    make_recipient: RecipientFactory,
) -> None:
    client = await make_recipient(email="client@example.com")
    await queue(session, client.user_id)

    taken = await worker.run_once()

    assert taken == 1
    [(address, message)] = email.sent
    assert address == "client@example.com"
    assert message.subject == "Вы записаны: Стрижка"
    [row] = await rows_of(session, client.user_id)
    assert (row.status, row.attempts, row.last_error) == ("sent", 1, None)
    assert row.sent_at is not None


async def test_telegram_goes_to_the_chat_linked_now(
    worker: DeliveryWorker,
    telegram: RecordingProvider,
    session: AsyncSession,
    make_recipient: RecipientFactory,
) -> None:
    client = await make_recipient(email_confirmed=False, telegram_chat_id=42)
    await queue(session, client.user_id)
    # Linked to another chat after the message was queued.
    recipients = RecipientRepository(session)
    current = await recipients.lock_or_create(client.user_id)
    await recipients.save(replace(current, telegram_chat_id=43))

    await worker.run_once()

    assert [address for address, _ in telegram.sent] == ["43"]


async def test_a_sent_message_is_not_sent_again(
    worker: DeliveryWorker,
    email: RecordingProvider,
    session: AsyncSession,
    make_recipient: RecipientFactory,
) -> None:
    client = await make_recipient()
    await queue(session, client.user_id)

    await worker.run_once()
    await worker.run_once()

    assert len(email.sent) == 1


async def test_a_temporary_failure_is_retried_later(
    worker: DeliveryWorker,
    email: RecordingProvider,
    clock: Clock,
    session: AsyncSession,
    make_recipient: RecipientFactory,
) -> None:
    client = await make_recipient()
    await queue(session, client.user_id)
    email.answers = [DeliveryResult.temporary("smtp: timeout")]

    await worker.run_once()

    [row] = await rows_of(session, client.user_id)
    assert (row.status, row.attempts, row.last_error) == ("pending", 1, "smtp: timeout")
    assert row.next_attempt_at == clock.now + timedelta(seconds=30)

    await worker.run_once()
    assert len(email.sent) == 1

    clock.now += timedelta(seconds=31)
    await worker.run_once()
    assert len(email.sent) == 2
    [row] = await rows_of(session, client.user_id)
    assert row.status == "sent"


async def test_a_rate_limit_waits_as_long_as_it_asks(
    worker: DeliveryWorker,
    email: RecordingProvider,
    clock: Clock,
    session: AsyncSession,
    make_recipient: RecipientFactory,
) -> None:
    client = await make_recipient()
    await queue(session, client.user_id)
    email.answers = [DeliveryResult.temporary("429", retry_after_seconds=120)]

    await worker.run_once()

    [row] = await rows_of(session, client.user_id)
    assert row.next_attempt_at == clock.now + timedelta(seconds=120)


async def test_temporary_failures_give_up_after_the_last_attempt(
    worker: DeliveryWorker,
    email: RecordingProvider,
    clock: Clock,
    session: AsyncSession,
    make_recipient: RecipientFactory,
) -> None:
    client = await make_recipient()
    await queue(session, client.user_id)
    email.answers = [DeliveryResult.temporary("down")] * 3

    for _ in range(3):
        await worker.run_once()
        clock.now += timedelta(hours=1)

    [row] = await rows_of(session, client.user_id)
    assert (row.status, row.attempts) == ("failed", 3)
    assert len(email.sent) == 3


async def test_a_permanent_failure_is_not_retried(
    worker: DeliveryWorker,
    email: RecordingProvider,
    clock: Clock,
    session: AsyncSession,
    make_recipient: RecipientFactory,
) -> None:
    client = await make_recipient()
    await queue(session, client.user_id)
    email.answers = [DeliveryResult.permanent("mailbox does not exist")]

    await worker.run_once()
    clock.now += timedelta(hours=1)
    await worker.run_once()

    [row] = await rows_of(session, client.user_id)
    assert (row.status, row.last_error) == ("failed", "mailbox does not exist")
    assert len(email.sent) == 1


async def test_a_blocked_bot_unlinks_the_chat(
    worker: DeliveryWorker,
    telegram: RecordingProvider,
    session: AsyncSession,
    make_recipient: RecipientFactory,
) -> None:
    client = await make_recipient(email_confirmed=False, telegram_chat_id=42)
    await queue(session, client.user_id)
    telegram.answers = [DeliveryResult.permanent("blocked", address_gone=True)]

    await worker.run_once()

    recipient = await RecipientRepository(session).get(client.user_id)
    assert recipient is not None
    assert recipient.telegram_chat_id is None


async def test_a_recipient_deactivated_since_the_message_was_queued_gets_nothing(
    worker: DeliveryWorker,
    email: RecordingProvider,
    session: AsyncSession,
    make_recipient: RecipientFactory,
) -> None:
    client = await make_recipient()
    await queue(session, client.user_id)
    recipients = RecipientRepository(session)
    current = await recipients.lock_or_create(client.user_id)
    await recipients.save(replace(current, is_active=False))

    await worker.run_once()

    assert email.sent == []
    [row] = await rows_of(session, client.user_id)
    assert row.status == "failed"


async def test_the_confirmation_letter_goes_to_its_address_and_forgets_the_link(
    worker: DeliveryWorker, email: RecordingProvider, session: AsyncSession
) -> None:
    user_id = uuid4()
    await EnqueueNotification(session).to_address(
        event_id=uuid4(),
        topic=AUTH_USERS_TOPIC,
        user_id=user_id,
        channel=Channel.EMAIL,
        address="new@example.com",
        template="email_confirmation",
        fields={"link": "http://x/confirm?token=secret", "expires_at": "tomorrow"},
    )

    await worker.run_once()

    [(address, message)] = email.sent
    assert address == "new@example.com"
    assert "token=secret" in message.body
    [row] = await rows_of(session, user_id)
    # The link was a credential; once it is delivered, the journal lets it go.
    assert (row.status, row.payload) == ("sent", {})


async def test_a_message_whose_worker_died_is_sent_after_the_lease(
    session_factory: async_sessionmaker[AsyncSession],
    make_worker: WorkerFactory,
    clock: Clock,
    session: AsyncSession,
    make_recipient: RecipientFactory,
) -> None:
    client = await make_recipient()
    await queue(session, client.user_id)
    dead = make_worker(session_factory)
    await dead._claim()  # took the row, then the pod died before sending

    survivor = make_worker(session_factory)
    assert await survivor.run_once() == 0

    clock.now += timedelta(seconds=61)
    assert await survivor.run_once() == 1
    [row] = await rows_of(session, client.user_id)
    assert (row.status, row.attempts) == ("sent", 2)


async def test_two_workers_never_send_the_same_message(
    concurrent_session_factory: async_sessionmaker[AsyncSession],
    make_worker: WorkerFactory,
    email: RecordingProvider,
    clock: Clock,
) -> None:
    async with concurrent_session_factory() as session:
        clients = []
        for index in range(10):
            recipient = Recipient(
                user_id=uuid4(), email=f"c{index}@example.com", email_confirmed=True
            )
            session.add(recipient)
            clients.append(recipient.user_id)
        await session.commit()
        for user_id in clients:
            await queue(session, user_id)
        await session.commit()
    # These rows commit for real, each at its own now(); on a loaded machine
    # that can be past the second of slack the clock started with.
    clock.now = datetime.now(UTC) + timedelta(seconds=1)

    # Small batches, so both workers really compete for the same rows.
    first = make_worker(concurrent_session_factory, batch_size=3)
    second = make_worker(concurrent_session_factory, batch_size=3)
    for _ in range(4):
        await asyncio.gather(first.run_once(), second.run_once())

    addresses = [address for address, _ in email.sent]
    assert sorted(addresses) == sorted(f"c{index}@example.com" for index in range(10))


async def test_a_booking_event_ends_as_a_message_to_the_client(
    session_factory: async_sessionmaker[AsyncSession],
    worker: DeliveryWorker,
    email: RecordingProvider,
    make_recipient: RecipientFactory,
) -> None:
    client = await make_recipient(email="client@example.com")
    consumer = EventConsumer(
        topics=BOOKING_EVENTS_TOPICS,
        group_id=BOOKING_EVENTS_GROUP,
        session_factory=session_factory,
        dead_letters=AsyncMock(),
        handlers=BookingEvents().handlers(),
        retry_policy=RetryPolicy(attempts=1),
        client=MagicMock(),
    )
    start_at = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    event = BookingCreated(
        booking_id=uuid4(),
        salon_id=uuid4(),
        master_id=uuid4(),
        client_user_id=client.user_id,
        service_id=uuid4(),
        service_name="Стрижка",
        price=Decimal("3500.00"),
        currency="RUB",
        start_at=start_at,
        end_at=start_at + timedelta(minutes=45),
        status="confirmed",
        timezone="Europe/Moscow",
    )
    envelope = build_envelope(
        event_type=BookingEventType.CREATED,
        payload=event.model_dump(mode="json"),
        producer="booking@test",
    )
    value = envelope.model_dump_json().encode("utf-8")
    message = ConsumerRecord(
        topic=BOOKING_EVENTS_TOPICS[0],
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

    await consumer.handle(message)
    await consumer.handle(message)
    await worker.run_once()

    [(address, sent)] = email.sent
    assert address == "client@example.com"
    assert "05.09.2026 15:00 (Europe/Moscow)" in sent.body
