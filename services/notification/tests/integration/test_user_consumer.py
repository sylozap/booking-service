"""Recipients follow auth.users.v1.

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

from barber_common.events.envelope import build_envelope
from barber_common.events.users import (
    AUTH_USERS_TOPIC,
    UserContactsUpdated,
    UserDeactivated,
    UserEmailConfirmationRequested,
    UserEmailConfirmed,
    UserEventType,
    UserRegistered,
)
from barber_common.kafka import EventConsumer, ProcessingResult, RetryPolicy
from barber_notification.consumers.user_events import (
    USER_EVENTS_GROUP,
    USER_EVENTS_TOPICS,
    UserEvents,
)
from barber_notification.models.notification import Notification
from barber_notification.models.recipient import Recipient
from barber_notification.repositories.recipients import RecipientRecord, RecipientRepository

pytestmark = pytest.mark.integration

RecipientFactory = Callable[..., Awaitable[Recipient]]

MORNING = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
CONFIRMATION_URL = "http://localhost:8080/confirm-email"


def record(
    event_type: str, payload: BaseModel, *, occurred_at: datetime = MORNING
) -> ConsumerRecord[bytes, bytes]:
    envelope = build_envelope(
        event_type=event_type,
        payload=payload.model_dump(mode="json"),
        producer="auth@test",
        occurred_at=occurred_at,
    )
    value = envelope.model_dump_json().encode("utf-8")
    return ConsumerRecord(
        topic=AUTH_USERS_TOPIC,
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


def registered(
    user_id: UUID, *, email: str = "a@example.com", occurred_at: datetime = MORNING
) -> ConsumerRecord[bytes, bytes]:
    return record(
        UserEventType.REGISTERED,
        UserRegistered(user_id=user_id, email=email, phone="+79990000000"),
        occurred_at=occurred_at,
    )


def contacts_updated(
    user_id: UUID, *, email: str, phone: str, occurred_at: datetime
) -> ConsumerRecord[bytes, bytes]:
    return record(
        UserEventType.CONTACTS_UPDATED,
        UserContactsUpdated(user_id=user_id, email=email, phone=phone, email_confirmed=False),
        occurred_at=occurred_at,
    )


@pytest.fixture
def consumer(session_factory: async_sessionmaker[AsyncSession]) -> EventConsumer:
    """The runner over a client that is never started: ``handle`` needs none."""
    return EventConsumer(
        topics=USER_EVENTS_TOPICS,
        group_id=USER_EVENTS_GROUP,
        session_factory=session_factory,
        dead_letters=AsyncMock(),
        handlers=UserEvents(confirmation_url=CONFIRMATION_URL).handlers(),
        retry_policy=RetryPolicy(attempts=1),
        client=MagicMock(),
    )


async def recipient_of(session: AsyncSession, user_id: UUID) -> RecipientRecord | None:
    return await RecipientRepository(session).get(user_id)


async def test_a_registration_creates_the_recipient(
    consumer: EventConsumer, session: AsyncSession
) -> None:
    user_id = uuid4()

    result = await consumer.handle(registered(user_id))

    assert result is ProcessingResult.OK
    recipient = await recipient_of(session, user_id)
    assert recipient is not None
    assert (recipient.email, recipient.phone) == ("a@example.com", "+79990000000")
    assert (recipient.email_confirmed, recipient.is_active) == (False, True)


async def test_new_contacts_replace_the_old_ones(
    consumer: EventConsumer, session: AsyncSession
) -> None:
    user_id = uuid4()
    await consumer.handle(registered(user_id))

    await consumer.handle(
        contacts_updated(
            user_id,
            email="b@example.com",
            phone="+79991111111",
            occurred_at=MORNING + timedelta(hours=1),
        )
    )

    recipient = await recipient_of(session, user_id)
    assert recipient is not None
    assert (recipient.email, recipient.phone) == ("b@example.com", "+79991111111")


async def test_a_redelivered_registration_changes_nothing(
    consumer: EventConsumer, session: AsyncSession
) -> None:
    user_id = uuid4()
    message = registered(user_id)

    first = await consumer.handle(message)
    second = await consumer.handle(message)

    assert (first, second) == (ProcessingResult.OK, ProcessingResult.DUPLICATE)
    assert await recipient_of(session, user_id) is not None


async def test_contacts_arriving_before_the_registration_are_not_lost(
    consumer: EventConsumer, session: AsyncSession
) -> None:
    user_id = uuid4()

    await consumer.handle(
        contacts_updated(
            user_id,
            email="b@example.com",
            phone="+79991111111",
            occurred_at=MORNING + timedelta(hours=1),
        )
    )
    await consumer.handle(registered(user_id, email="a@example.com", occurred_at=MORNING))

    recipient = await recipient_of(session, user_id)
    assert recipient is not None
    assert (recipient.email, recipient.phone) == ("b@example.com", "+79991111111")


async def test_a_confirmation_marks_the_address(
    consumer: EventConsumer, session: AsyncSession
) -> None:
    user_id = uuid4()
    await consumer.handle(registered(user_id))

    await consumer.handle(
        record(
            UserEventType.EMAIL_CONFIRMED,
            UserEmailConfirmed(user_id=user_id, email="a@example.com"),
        )
    )

    recipient = await recipient_of(session, user_id)
    assert recipient is not None
    assert recipient.email_confirmed is True


async def test_a_confirmation_before_the_registration_survives_it(
    consumer: EventConsumer, session: AsyncSession
) -> None:
    user_id = uuid4()

    await consumer.handle(
        record(
            UserEventType.EMAIL_CONFIRMED,
            UserEmailConfirmed(user_id=user_id, email="a@example.com"),
            occurred_at=MORNING + timedelta(minutes=10),
        )
    )
    await consumer.handle(registered(user_id, occurred_at=MORNING))

    recipient = await recipient_of(session, user_id)
    assert recipient is not None
    assert (recipient.email, recipient.email_confirmed) == ("a@example.com", True)


async def test_a_deactivated_account_stops_receiving(
    consumer: EventConsumer, session: AsyncSession, make_recipient: RecipientFactory
) -> None:
    existing = await make_recipient()

    await consumer.handle(
        record(UserEventType.DEACTIVATED, UserDeactivated(user_id=existing.user_id))
    )

    recipient = await recipient_of(session, existing.user_id)
    assert recipient is not None
    assert recipient.is_active is False


async def test_a_deactivation_is_not_undone_by_a_late_registration(
    consumer: EventConsumer, session: AsyncSession
) -> None:
    user_id = uuid4()

    await consumer.handle(record(UserEventType.DEACTIVATED, UserDeactivated(user_id=user_id)))
    await consumer.handle(registered(user_id))

    recipient = await recipient_of(session, user_id)
    assert recipient is not None
    assert (recipient.email, recipient.is_active) == ("a@example.com", False)


async def test_an_event_type_without_a_handler_is_skipped(consumer: EventConsumer) -> None:
    result = await consumer.handle(record("user.renamed", UserDeactivated(user_id=uuid4())))

    assert result is ProcessingResult.SKIPPED


# --- the confirmation letter --------------------------------------------------


def confirmation_requested(user_id: UUID, *, email: str) -> ConsumerRecord[bytes, bytes]:
    return record(
        UserEventType.EMAIL_CONFIRMATION_REQUESTED,
        UserEmailConfirmationRequested(
            user_id=user_id,
            email=email,
            token="kJ9-token",  # noqa: S106 - a fixture, not a credential
            expires_at="2026-09-02T09:00:00+00:00",
        ),
    )


async def letters_to(session: AsyncSession, user_id: UUID) -> list[Notification]:
    statement = select(Notification).where(Notification.user_id == user_id)
    return list((await session.execute(statement)).scalars().all())


async def test_a_confirmation_letter_is_queued_to_the_address_being_confirmed(
    consumer: EventConsumer, session: AsyncSession
) -> None:
    user_id = uuid4()

    await consumer.handle(confirmation_requested(user_id, email="new@example.com"))

    [letter] = await letters_to(session, user_id)
    assert (letter.channel, letter.template, letter.address) == (
        "email",
        "email_confirmation",
        "new@example.com",
    )
    assert letter.payload["link"] == f"{CONFIRMATION_URL}?token=kJ9-token"


async def test_the_letter_ignores_preferences_but_not_a_deactivation(
    consumer: EventConsumer, session: AsyncSession, make_recipient: RecipientFactory
) -> None:
    switched_off = await make_recipient(preferences={"channels": {"email": False}})
    deactivated = await make_recipient(is_active=False)

    await consumer.handle(confirmation_requested(switched_off.user_id, email="a@example.com"))
    await consumer.handle(confirmation_requested(deactivated.user_id, email="b@example.com"))

    assert len(await letters_to(session, switched_off.user_id)) == 1
    assert await letters_to(session, deactivated.user_id) == []


async def test_a_redelivered_request_queues_one_letter(
    consumer: EventConsumer, session: AsyncSession
) -> None:
    user_id = uuid4()
    message = confirmation_requested(user_id, email="a@example.com")

    await consumer.handle(message)
    await consumer.handle(message)

    assert len(await letters_to(session, user_id)) == 1
