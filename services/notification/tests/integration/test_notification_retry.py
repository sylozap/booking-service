"""What becomes of a notification that cannot be sent.

A temporary failure is retried with doubling pauses; once the attempts run out,
or at once for a permanent failure, the row turns ``failed`` and a record of it
is written to the outbox, bound for the dead letter topic of the event behind
the notification. The provider is the only stand-in.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from barber_common.events.bookings import BOOKINGS_TOPIC
from barber_common.events.notifications import NotificationEventType
from barber_common.events.users import AUTH_USERS_TOPIC
from barber_common.metrics import REGISTRY
from barber_common.outbox.models import OutboxMessage
from barber_notification.models.notification import Notification
from barber_notification.models.recipient import Recipient
from barber_notification.preferences import NotificationKind
from barber_notification.providers.base import Channel, DeliveryResult, Message
from barber_notification.services.dispatch import EnqueueNotification
from barber_notification.workers.delivery import DeliveryWorker

pytestmark = pytest.mark.integration

RecipientFactory = Callable[..., Awaitable[Recipient]]
WorkerFactory = Callable[..., DeliveryWorker]

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


async def queue(session: AsyncSession, user_id: UUID) -> UUID:
    event_id = uuid4()
    await EnqueueNotification(session).execute(
        event_id=event_id,
        topic=BOOKINGS_TOPIC,
        user_id=user_id,
        kind=NotificationKind.BOOKINGS,
        template="booking_created",
        fields=FIELDS,
    )
    return event_id


async def notification_of(session: AsyncSession, event_id: UUID) -> Notification:
    statement = (
        select(Notification)
        .where(Notification.event_id == event_id)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(statement)).scalar_one()


async def dead_letters(session: AsyncSession) -> list[OutboxMessage]:
    statement = select(OutboxMessage).where(
        OutboxMessage.event_type == NotificationEventType.DELIVERY_FAILED
    )
    return list((await session.execute(statement)).scalars().all())


def dlq_count(topic: str, reason: str) -> float:
    value = REGISTRY.get_sample_value("dlq_messages_total", {"topic": topic, "reason": reason})
    return value or 0.0


async def test_a_temporary_failure_is_retried_with_doubling_pauses(
    worker: DeliveryWorker,
    email: RecordingProvider,
    clock: Clock,
    session: AsyncSession,
    make_recipient: RecipientFactory,
) -> None:
    client = await make_recipient()
    event_id = await queue(session, client.user_id)
    email.answers = [DeliveryResult.temporary("smtp: timeout")] * 2
    pauses = []

    for _ in range(2):
        before = clock.now
        await worker.run_once()
        row = await notification_of(session, event_id)
        pauses.append(row.next_attempt_at - before)
        clock.now = row.next_attempt_at

    assert pauses == [timedelta(seconds=30), timedelta(seconds=60)]
    assert await dead_letters(session) == []


async def test_temporary_failures_go_to_the_dead_letter_topic_after_the_last_attempt(
    worker: DeliveryWorker,
    email: RecordingProvider,
    clock: Clock,
    session: AsyncSession,
    make_recipient: RecipientFactory,
) -> None:
    client = await make_recipient()
    event_id = await queue(session, client.user_id)
    email.answers = [DeliveryResult.temporary("smtp: timeout")] * 3

    for _ in range(4):
        await worker.run_once()
        clock.now += timedelta(hours=1)

    assert len(email.sent) == 3
    row = await notification_of(session, event_id)
    assert (row.status, row.attempts, row.last_error) == ("failed", 3, "smtp: timeout")
    [letter] = await dead_letters(session)
    assert letter.topic == f"{BOOKINGS_TOPIC}.dlq"
    assert (letter.aggregate_id, letter.causation_id) == (row.id, event_id)
    assert letter.payload == {
        "notification_id": str(row.id),
        "original_event_id": str(event_id),
        "user_id": str(client.user_id),
        "channel": "email",
        "template": "booking_created",
        "dlq_reason": "retries_exhausted",
        "dlq_detail": "smtp: timeout",
        "dlq_attempts": 3,
        "dlq_original_topic": BOOKINGS_TOPIC,
        "dlq_at": letter.payload["dlq_at"],
    }


async def test_a_permanent_failure_goes_to_the_dead_letter_topic_without_a_retry(
    worker: DeliveryWorker,
    email: RecordingProvider,
    clock: Clock,
    session: AsyncSession,
    make_recipient: RecipientFactory,
) -> None:
    client = await make_recipient()
    event_id = await queue(session, client.user_id)
    email.answers = [DeliveryResult.permanent("mailbox does not exist")]

    await worker.run_once()
    clock.now += timedelta(hours=1)
    await worker.run_once()

    assert len(email.sent) == 1
    row = await notification_of(session, event_id)
    assert (row.status, row.attempts) == ("failed", 1)
    [letter] = await dead_letters(session)
    assert letter.payload["dlq_reason"] == "permanent_failure"
    assert letter.payload["dlq_detail"] == "mailbox does not exist"
    assert letter.payload["dlq_attempts"] == 1


async def test_a_sent_message_writes_no_dead_letter(
    worker: DeliveryWorker, session: AsyncSession, make_recipient: RecipientFactory
) -> None:
    client = await make_recipient()
    await queue(session, client.user_id)

    await worker.run_once()

    assert await dead_letters(session) == []


async def test_the_failed_mark_and_the_dead_letter_are_one_transaction(
    concurrent_session_factory: async_sessionmaker[AsyncSession],
    make_worker: WorkerFactory,
    email: RecordingProvider,
    clock: Clock,
) -> None:
    async with concurrent_session_factory() as session:
        client = Recipient(user_id=uuid4(), email="c@example.com", email_confirmed=True)
        session.add(client)
        await session.commit()
        event_id = await queue(session, client.user_id)
        await session.commit()
    clock.now = datetime.now(UTC) + timedelta(seconds=1)
    email.answers = [DeliveryResult.permanent("mailbox does not exist")]

    await make_worker(concurrent_session_factory).run_once()

    async with concurrent_session_factory() as session:
        row = await notification_of(session, event_id)
        [letter] = await dead_letters(session)
    # now() is the start of the transaction, so the two stamps are equal only
    # if both writes were made by one; the claim, a transaction earlier, set
    # another one.
    assert row.status == "failed"
    assert row.updated_at == letter.created_at


async def test_a_dead_letter_is_counted_by_topic_and_reason(
    worker: DeliveryWorker,
    email: RecordingProvider,
    session: AsyncSession,
    make_recipient: RecipientFactory,
) -> None:
    client = await make_recipient()
    await queue(session, client.user_id)
    email.answers = [DeliveryResult.permanent("mailbox does not exist")]
    before = dlq_count(BOOKINGS_TOPIC, "permanent_failure")

    await worker.run_once()

    assert dlq_count(BOOKINGS_TOPIC, "permanent_failure") == before + 1


async def test_the_dead_letter_of_a_confirmation_letter_keeps_no_link(
    worker: DeliveryWorker, email: RecordingProvider, session: AsyncSession
) -> None:
    await EnqueueNotification(session).to_address(
        event_id=uuid4(),
        topic=AUTH_USERS_TOPIC,
        user_id=uuid4(),
        channel=Channel.EMAIL,
        address="new@example.com",
        template="email_confirmation",
        fields={"link": "http://x/confirm?token=secret", "expires_at": "tomorrow"},
    )
    email.answers = [DeliveryResult.permanent("mailbox does not exist")]

    await worker.run_once()

    [letter] = await dead_letters(session)
    assert letter.topic == f"{AUTH_USERS_TOPIC}.dlq"
    assert "secret" not in str(letter.payload)
