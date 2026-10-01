"""The metrics of delivery, read where Prometheus reads them."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from barber_common.events.bookings import BOOKINGS_TOPIC
from barber_common.metrics import REGISTRY
from barber_notification.models.recipient import Recipient
from barber_notification.preferences import NotificationKind
from barber_notification.providers.base import DeliveryResult, Message
from barber_notification.services.dispatch import EnqueueNotification
from barber_notification.workers.delivery import DeliveryWorker

pytestmark = pytest.mark.integration

RecipientFactory = Callable[..., Awaitable[Recipient]]
WorkerFactory = Callable[..., DeliveryWorker]


class RecordingProvider(Protocol):
    """The provider of conftest: what it was given, and what it answers next."""

    sent: list[tuple[str, Message]]
    answers: list[DeliveryResult]


async def queue(session: AsyncSession, user_id: UUID) -> None:
    await EnqueueNotification(session).execute(
        event_id=uuid4(),
        topic=BOOKINGS_TOPIC,
        user_id=user_id,
        kind=NotificationKind.BOOKINGS,
        template="booking_created",
        fields={"service_name": "Стрижка", "start_at": "05.09.2026 15:00"},
    )


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def sent(channel: str, result: str) -> float:
    return sample("notifications_sent_total", channel=channel, result=result)


async def test_a_delivered_notification_counts_on_its_channel(
    worker: DeliveryWorker, session: AsyncSession, make_recipient: RecipientFactory
) -> None:
    client = await make_recipient(telegram_chat_id=42)
    await queue(session, client.user_id)
    before = {channel: sent(channel, "sent") for channel in ("email", "telegram")}

    await worker.run_once()

    assert sent("email", "sent") == before["email"] + 1
    assert sent("telegram", "sent") == before["telegram"] + 1


@pytest.mark.parametrize(
    ("answer", "result"),
    [
        (DeliveryResult.temporary("telegram: 502"), "temporary_failure"),
        (DeliveryResult.permanent("bot blocked"), "permanent_failure"),
    ],
)
async def test_a_failure_counts_by_its_kind(
    worker: DeliveryWorker,
    telegram: RecordingProvider,
    session: AsyncSession,
    make_recipient: RecipientFactory,
    answer: DeliveryResult,
    result: str,
) -> None:
    client = await make_recipient(email_confirmed=False, telegram_chat_id=42)
    await queue(session, client.user_id)
    telegram.answers = [answer]
    before = sent("telegram", result)

    await worker.run_once()

    assert sent("telegram", result) == before + 1


async def test_a_notification_with_no_address_counts_as_a_permanent_failure(
    worker: DeliveryWorker,
    email: RecordingProvider,
    session: AsyncSession,
    make_recipient: RecipientFactory,
) -> None:
    client = await make_recipient()
    await queue(session, client.user_id)
    # The address went away after the message was queued.
    await session.execute(
        update(Recipient).where(Recipient.user_id == client.user_id).values(email=None)
    )
    before = sent("email", "permanent_failure")

    await worker.run_once()

    assert email.sent == []
    assert sent("email", "permanent_failure") == before + 1


async def test_each_call_to_a_provider_is_timed_by_channel(
    worker: DeliveryWorker, session: AsyncSession, make_recipient: RecipientFactory
) -> None:
    client = await make_recipient()
    await queue(session, client.user_id)
    before = sample("notification_send_duration_seconds_count", channel="email")

    await worker.run_once()

    assert sample("notification_send_duration_seconds_count", channel="email") == before + 1


async def test_the_queue_is_measured_as_a_pass_leaves_it(
    session_factory: async_sessionmaker[AsyncSession],
    make_worker: WorkerFactory,
    session: AsyncSession,
    make_recipient: RecipientFactory,
) -> None:
    for _ in range(3):
        client = await make_recipient()
        await queue(session, client.user_id)
    worker = make_worker(session_factory, batch_size=1)

    await worker.run_once()

    assert sample("notifications_pending_messages") == 2
    # The two left behind have been due since they were queued.
    assert sample("notifications_oldest_due_age_seconds") > 0


async def test_an_empty_queue_has_no_age(
    worker: DeliveryWorker, session: AsyncSession, make_recipient: RecipientFactory
) -> None:
    client = await make_recipient()
    await queue(session, client.user_id)

    await worker.run_once()

    assert sample("notifications_pending_messages") == 0
    assert sample("notifications_oldest_due_age_seconds") == 0


async def test_no_identifier_is_a_label(
    worker: DeliveryWorker, session: AsyncSession, make_recipient: RecipientFactory
) -> None:
    client = await make_recipient()
    await queue(session, client.user_id)

    await worker.run_once()

    labels = {
        frozenset(sample.labels)
        for family in REGISTRY.collect()
        for sample in family.samples
        if sample.name == "notifications_sent_total"
    }
    assert labels == {frozenset({"channel", "result"})}
