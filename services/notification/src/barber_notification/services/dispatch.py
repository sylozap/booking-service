"""Deciding who gets a message on which channels, and queueing it.

Runs in the transaction of the consumer that received the event, next to the
row that marks the event processed. Nothing is sent here: a network call does
not belong inside a transaction, and the delivery worker sends what is queued.

Which channels a message goes to:

* every channel the recipient has not switched off and can be reached on --
  ``email`` with a confirmed address, ``telegram`` with a linked chat;
* a message the platform must deliver whatever the preferences -- the
  confirmation letter -- goes to the one address its event names.

A recipient this service has not heard of, or one that was deactivated, gets
nothing; that is logged and the event is done with.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from barber_common.db.session import transaction
from barber_common.logging import get_logger
from barber_notification.providers.base import Channel
from barber_notification.rendering import render
from barber_notification.repositories.notifications import NotificationRepository
from barber_notification.repositories.recipients import RecipientRecord, RecipientRepository

__all__ = ["EnqueueNotification", "channel_enabled", "reachable_channels"]

_logger = get_logger(__name__)


def channel_enabled(preferences: dict[str, object], channel: Channel) -> bool:
    """Whether the user left this channel on. Everything is on until switched off.

    ``{"channels": {"telegram": false}}`` switches Telegram off. Anything that
    is not a plain ``false`` leaves the channel on: a preference this code
    cannot read must not silence a user.
    """
    channels = preferences.get("channels")
    if not isinstance(channels, dict):
        return True
    return channels.get(channel.value) is not False


def reachable_channels(recipient: RecipientRecord) -> list[Channel]:
    """The channels a message to this recipient goes to, in a fixed order."""
    if not recipient.is_active:
        return []
    reachable: list[Channel] = []
    if recipient.email is not None and recipient.email_confirmed:
        reachable.append(Channel.EMAIL)
    if recipient.telegram_chat_id is not None:
        reachable.append(Channel.TELEGRAM)
    return [channel for channel in reachable if channel_enabled(recipient.preferences, channel)]


class EnqueueNotification:
    """Queue one message about one event for one user."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._recipients = RecipientRepository(session)
        self._notifications = NotificationRepository(session)

    async def execute(
        self,
        *,
        event_id: UUID,
        user_id: UUID,
        template: str,
        fields: dict[str, object],
    ) -> list[Channel]:
        """Queue it on every channel the user can be reached on. Returns them."""
        # Rendered once here so a template the payload cannot fill fails now,
        # with the event, rather than later and silently in the worker.
        render(template, fields)

        async with transaction(self._session):
            recipient = await self._recipients.get(user_id)
            if recipient is None:
                _logger.info(
                    "no recipient for this user yet; nothing sent",
                    user_id=str(user_id),
                    template=template,
                )
                return []

            channels = reachable_channels(recipient)
            await self._queue(
                event_id=event_id,
                user_id=user_id,
                template=template,
                fields=fields,
                channels=channels,
            )

        _logger.info(
            "notification queued",
            user_id=str(user_id),
            template=template,
            channels=[channel.value for channel in channels],
        )
        return channels

    async def to_address(
        self,
        *,
        event_id: UUID,
        user_id: UUID,
        channel: Channel,
        address: str,
        template: str,
        fields: dict[str, object],
    ) -> bool:
        """Queue a message that must reach this very address. Whether it is new.

        Preferences do not apply, and the recipient row need not exist yet; a
        deactivated account still gets nothing.
        """
        render(template, fields)

        async with transaction(self._session):
            recipient = await self._recipients.get(user_id)
            if recipient is not None and not recipient.is_active:
                _logger.info("recipient is deactivated; nothing sent", user_id=str(user_id))
                return False
            queued = await self._notifications.add_pending(
                user_id=user_id,
                event_id=event_id,
                channel=channel.value,
                template=template,
                payload=fields,
                address=address,
            )

        _logger.info("notification queued", user_id=str(user_id), template=template)
        return queued

    async def _queue(
        self,
        *,
        event_id: UUID,
        user_id: UUID,
        template: str,
        fields: dict[str, object],
        channels: Sequence[Channel],
    ) -> None:
        for channel in channels:
            await self._notifications.add_pending(
                user_id=user_id,
                event_id=event_id,
                channel=channel.value,
                template=template,
                payload=fields,
            )
