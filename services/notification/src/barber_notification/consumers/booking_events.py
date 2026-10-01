"""Bookings, as their clients are told about them, from ``booking.bookings.v1``
and ``booking.reminders.v1``.

A creation, a move and a cancellation become a message, and so does a reminder
the scheduler of booking published when its time came. A visit closed as
completed or missed does not: the client was there, or chose not to be, and
has nothing to learn. Those types have no handler, and the runner logs and
commits them.

Each handler only queues: it runs in the runner's transaction, together with
the row that marks the event processed, and the delivery worker sends later.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from barber_common.events.bookings import (
    BOOKINGS_TOPIC,
    REMINDERS_TOPIC,
    BookingCancelled,
    BookingCreated,
    BookingEventType,
    BookingRescheduled,
    ReminderDue,
    ReminderEventType,
)
from barber_common.events.envelope import JsonEnvelope
from barber_common.kafka import EventHandler
from barber_notification.preferences import NotificationKind
from barber_notification.services.booking_messages import (
    TemplateAndFields,
    cancelled_message,
    created_message,
    reminder_message,
    rescheduled_message,
)
from barber_notification.services.dispatch import EnqueueNotification

__all__ = ["BOOKING_EVENTS_GROUP", "BOOKING_EVENTS_TOPICS", "BookingEvents"]

BOOKING_EVENTS_GROUP = "notification.bookings"
# One group for both: a reminder is a message about a booking like any other,
# and the volume of either is far from holding the other back.
BOOKING_EVENTS_TOPICS = (BOOKINGS_TOPIC, REMINDERS_TOPIC)


class BookingEvents:
    """The handlers that turn booking events into messages to the client."""

    def handlers(self) -> dict[str, EventHandler]:
        """Event types this group reacts to, and how."""
        return {
            BookingEventType.CREATED: self.created,
            BookingEventType.RESCHEDULED: self.rescheduled,
            BookingEventType.CANCELLED: self.cancelled,
            ReminderEventType.DUE: self.reminder_due,
        }

    async def created(self, session: AsyncSession, envelope: JsonEnvelope) -> None:
        event = BookingCreated.model_validate(envelope.payload)
        await _notify(session, envelope, event.client_user_id, created_message(event))

    async def rescheduled(self, session: AsyncSession, envelope: JsonEnvelope) -> None:
        event = BookingRescheduled.model_validate(envelope.payload)
        await _notify(session, envelope, event.client_user_id, rescheduled_message(event))

    async def cancelled(self, session: AsyncSession, envelope: JsonEnvelope) -> None:
        event = BookingCancelled.model_validate(envelope.payload)
        await _notify(session, envelope, event.client_user_id, cancelled_message(event))

    async def reminder_due(self, session: AsyncSession, envelope: JsonEnvelope) -> None:
        event = ReminderDue.model_validate(envelope.payload)
        await _notify(
            session,
            envelope,
            event.client_user_id,
            reminder_message(event),
            topic=REMINDERS_TOPIC,
            kind=NotificationKind.REMINDERS,
        )


async def _notify(
    session: AsyncSession,
    envelope: JsonEnvelope,
    client_user_id: UUID,
    message: TemplateAndFields,
    *,
    topic: str = BOOKINGS_TOPIC,
    kind: NotificationKind = NotificationKind.BOOKINGS,
) -> None:
    template, fields = message
    await EnqueueNotification(session).execute(
        event_id=envelope.event_id,
        topic=topic,
        user_id=client_user_id,
        kind=kind,
        template=template,
        fields=fields,
    )
