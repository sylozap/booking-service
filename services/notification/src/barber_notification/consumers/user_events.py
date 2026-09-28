"""Accounts, as notification follows them from ``auth.users.v1``.

The table of recipients is filled here and nowhere else: a notification has to
go out while ``auth`` is down, so this service never asks it for contacts. The
confirmation letter starts here too, addressed to the address the event names.

Each handler runs in the runner's transaction, together with the row that marks
the event processed: a redelivered event is dropped before it gets here.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from barber_common.events.envelope import JsonEnvelope
from barber_common.events.users import (
    AUTH_USERS_TOPIC,
    UserContactsUpdated,
    UserDeactivated,
    UserEmailConfirmationRequested,
    UserEmailConfirmed,
    UserEventType,
    UserRegistered,
)
from barber_common.kafka import EventHandler
from barber_notification.providers.base import Channel
from barber_notification.services.account_messages import confirmation_message
from barber_notification.services.dispatch import EnqueueNotification
from barber_notification.services.recipients import FollowUser

__all__ = ["USER_EVENTS_GROUP", "USER_EVENTS_TOPICS", "UserEvents"]

USER_EVENTS_GROUP = "notification.users"
USER_EVENTS_TOPICS = (AUTH_USERS_TOPIC,)


class UserEvents:
    """The handlers that keep recipients in step with auth."""

    def __init__(self, *, confirmation_url: str) -> None:
        self._confirmation_url = confirmation_url

    def handlers(self) -> dict[str, EventHandler]:
        """Event types this group reacts to, and how."""
        return {
            UserEventType.REGISTERED: self.registered,
            UserEventType.EMAIL_CONFIRMATION_REQUESTED: self.email_confirmation_requested,
            UserEventType.CONTACTS_UPDATED: self.contacts_updated,
            UserEventType.EMAIL_CONFIRMED: self.email_confirmed,
            UserEventType.DEACTIVATED: self.deactivated,
        }

    async def registered(self, session: AsyncSession, envelope: JsonEnvelope) -> None:
        await FollowUser(session).registered(
            UserRegistered.model_validate(envelope.payload), occurred_at=envelope.occurred_at
        )

    async def contacts_updated(self, session: AsyncSession, envelope: JsonEnvelope) -> None:
        await FollowUser(session).contacts_updated(
            UserContactsUpdated.model_validate(envelope.payload), occurred_at=envelope.occurred_at
        )

    async def email_confirmation_requested(
        self, session: AsyncSession, envelope: JsonEnvelope
    ) -> None:
        """Queue the letter with the link. The token is never logged."""
        event = UserEmailConfirmationRequested.model_validate(envelope.payload)
        template, fields = confirmation_message(event, confirmation_url=self._confirmation_url)
        await EnqueueNotification(session).to_address(
            event_id=envelope.event_id,
            user_id=event.user_id,
            channel=Channel.EMAIL,
            address=event.email,
            template=template,
            fields=fields,
        )

    async def email_confirmed(self, session: AsyncSession, envelope: JsonEnvelope) -> None:
        await FollowUser(session).email_confirmed(
            UserEmailConfirmed.model_validate(envelope.payload)
        )

    async def deactivated(self, session: AsyncSession, envelope: JsonEnvelope) -> None:
        await FollowUser(session).deactivated(UserDeactivated.model_validate(envelope.payload))
