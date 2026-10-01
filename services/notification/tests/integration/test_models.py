"""Database constraints of the notification schema, on a real PostgreSQL."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from barber_common.db.errors import (
    SQLSTATE_CHECK_VIOLATION,
    SQLSTATE_UNIQUE_VIOLATION,
    sqlstate_of,
)
from barber_notification.models.notification import Notification
from barber_notification.models.recipient import Recipient

pytestmark = pytest.mark.integration


def notification(
    *,
    event_id: UUID | None = None,
    channel: str = "email",
    status: str = "pending",
    attempts: int = 0,
) -> Notification:
    event = event_id or uuid4()
    return Notification(
        user_id=uuid4(),
        event_id=event,
        topic="booking.bookings.v1",
        channel=channel,
        template="booking_created",
        payload={"service_name": "Haircut"},
        dedup_key=f"{event}:{channel}",
        status=status,
        attempts=attempts,
    )


async def flush_error(session: AsyncSession) -> str | None:
    with pytest.raises(IntegrityError) as failure:
        await session.flush()
    return sqlstate_of(failure.value)


async def test_a_second_notification_with_the_same_dedup_key_is_refused(
    session: AsyncSession,
) -> None:
    event_id = uuid4()
    session.add(notification(event_id=event_id))
    await session.flush()

    session.add(notification(event_id=event_id))

    assert await flush_error(session) == SQLSTATE_UNIQUE_VIOLATION


async def test_the_same_event_on_another_channel_is_a_notification_of_its_own(
    session: AsyncSession,
) -> None:
    event_id = uuid4()
    session.add(notification(event_id=event_id, channel="email"))
    session.add(notification(event_id=event_id, channel="telegram"))

    await session.flush()


async def test_an_unknown_channel_is_refused(session: AsyncSession) -> None:
    session.add(notification(channel="pigeon"))

    assert await flush_error(session) == SQLSTATE_CHECK_VIOLATION


async def test_an_unknown_status_is_refused(session: AsyncSession) -> None:
    session.add(notification(status="lost"))

    assert await flush_error(session) == SQLSTATE_CHECK_VIOLATION


async def test_a_negative_attempt_count_is_refused(session: AsyncSession) -> None:
    session.add(notification(attempts=-1))

    assert await flush_error(session) == SQLSTATE_CHECK_VIOLATION


async def test_a_new_notification_waits_to_be_sent(session: AsyncSession) -> None:
    row = Notification(
        user_id=uuid4(),
        event_id=uuid4(),
        topic="booking.bookings.v1",
        channel="email",
        template="booking_created",
        payload={},
        dedup_key=f"{uuid4()}:email",
    )
    session.add(row)

    await session.flush()

    assert (row.status, row.attempts, row.sent_at) == ("pending", 0, None)
    assert row.next_attempt_at is not None


async def test_a_recipient_can_exist_before_its_contacts_are_known(
    session: AsyncSession,
) -> None:
    recipient = Recipient(user_id=uuid4(), telegram_chat_id=123456789)
    session.add(recipient)

    await session.flush()

    assert (recipient.email, recipient.email_confirmed, recipient.is_active) == (
        None,
        False,
        True,
    )
    assert recipient.preferences == {}
