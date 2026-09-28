"""Sending what the consumers queued.

A pass has three steps, and only the middle one talks to the outside world:

1. a transaction claims a batch of due notifications (``SKIP LOCKED``, so two
   replicas take different rows), marks them ``sending`` with a lease, and
   reads where each recipient can be reached now;
2. with no transaction open, each message is rendered and handed to the
   provider of its channel;
3. a transaction per notification records the outcome: ``sent``, a retry
   later, or ``failed``.

A pod that dies between 2 and 3 leaves its rows ``sending``; when the lease
runs out another pass takes them again. The message may then go out twice --
the one duplicate at-least-once delivery allows -- but it is never lost. A
redelivered *event* never makes a second message: the dedup key refused it
when it was queued.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from barber_common.db.session import unit_of_work
from barber_common.logging import get_logger
from barber_notification.providers.base import Channel, DeliveryOutcome, DeliveryResult
from barber_notification.providers.registry import ProviderRegistry
from barber_notification.rendering import SENSITIVE_TEMPLATES, TemplateFieldMissing, render
from barber_notification.repositories.notifications import (
    NotificationRecord,
    NotificationRepository,
)
from barber_notification.repositories.recipients import RecipientRecord, RecipientRepository

__all__ = ["DeliveryWorker", "address_of"]

_logger = get_logger(__name__)

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def address_of(notification: NotificationRecord, recipient: RecipientRecord | None) -> str | None:
    """Where this notification goes now, or nothing if nowhere.

    An address the event named wins. Otherwise it is the recipient's current
    one: a chat linked or an address changed since the message was queued is
    the one that works today.
    """
    if notification.address is not None:
        return notification.address
    if recipient is None:
        return None
    if notification.channel == Channel.EMAIL.value:
        return recipient.email
    if notification.channel == Channel.TELEGRAM.value and recipient.telegram_chat_id is not None:
        return str(recipient.telegram_chat_id)
    return None


@dataclass(frozen=True, slots=True)
class _Job:
    notification: NotificationRecord
    address: str | None
    recipient_active: bool


class DeliveryWorker:
    """Sends due notifications through the provider of their channel."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        providers: ProviderRegistry,
        clock: Clock = _utc_now,
        batch_size: int = 20,
        lease: timedelta = timedelta(seconds=60),
        max_attempts: int = 3,
        retry_delay: timedelta = timedelta(seconds=30),
        idle_interval_seconds: float = 1.0,
    ) -> None:
        self._session_factory = session_factory
        self._providers = providers
        self._clock = clock
        self._batch_size = batch_size
        self._lease = lease
        self._max_attempts = max_attempts
        self._retry_delay = retry_delay
        self._idle_interval_seconds = idle_interval_seconds

    async def run_once(self) -> int:
        """One pass. Returns how many notifications it took."""
        jobs = await self._claim()
        for job in jobs:
            result = await self._send(job)
            await self._record(job, result)
        return len(jobs)

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Keep delivering until asked to stop.

        A failed pass is logged and retried: the rows it held are released by
        their lease, not lost.
        """
        _logger.info("delivery worker started", batch_size=self._batch_size)
        while not stop.is_set():
            try:
                taken = await self.run_once()
            except Exception:
                _logger.exception("delivery pass failed")
                taken = 0
            if taken >= self._batch_size:
                continue
            try:
                async with asyncio.timeout(self._idle_interval_seconds):
                    await stop.wait()
            except TimeoutError:
                continue
        _logger.info("delivery worker stopped")

    @asynccontextmanager
    async def run_in_background(self) -> AsyncIterator[None]:
        """Run the worker for as long as the block lasts.

        The task is held in a local variable rather than created and forgotten:
        a task nobody references can be collected mid-flight and its exception
        never surfaces.
        """
        stop = asyncio.Event()
        task = asyncio.create_task(self.run_forever(stop), name="notification-delivery")
        try:
            yield
        finally:
            stop.set()
            await task

    async def _claim(self) -> list[_Job]:
        async with unit_of_work(self._session_factory) as session:
            notifications = await NotificationRepository(session).claim_due(
                now=self._clock(), limit=self._batch_size, lease=self._lease
            )
            recipients = RecipientRepository(session)
            jobs = []
            for notification in notifications:
                recipient = await recipients.get(notification.user_id)
                jobs.append(
                    _Job(
                        notification=notification,
                        address=address_of(notification, recipient),
                        recipient_active=recipient is None or recipient.is_active,
                    )
                )
        return jobs

    async def _send(self, job: _Job) -> DeliveryResult:
        """Render and deliver, outside any transaction."""
        notification = job.notification
        if not job.recipient_active:
            return DeliveryResult.permanent("recipient is deactivated")
        if job.address is None:
            return DeliveryResult.permanent(f"no {notification.channel} address for the recipient")
        try:
            message = render(notification.template, notification.payload)
        except TemplateFieldMissing as error:
            return DeliveryResult.permanent(str(error))

        # A provider reports failures as results. One that raises anyway fails
        # the pass; the row stays ``sending`` and its lease brings it back.
        provider = self._providers.for_channel(Channel(notification.channel))
        return await provider.send(job.address, message)

    async def _record(self, job: _Job, result: DeliveryResult) -> None:
        notification = job.notification
        now = self._clock()
        async with unit_of_work(self._session_factory) as session:
            journal = NotificationRepository(session)
            if result.outcome is DeliveryOutcome.SENT:
                await journal.mark_sent(
                    notification.id,
                    now=now,
                    forget_payload=notification.template in SENSITIVE_TEMPLATES,
                )
            elif (
                result.outcome is DeliveryOutcome.TEMPORARY_FAILURE
                and notification.attempts < self._max_attempts
            ):
                await journal.retry_at(
                    notification.id,
                    at=now + self._pause_after(notification.attempts, result),
                    error=result.detail or "temporary failure",
                )
            else:
                await journal.mark_failed(notification.id, error=result.detail or "failed")

            if result.address_gone and notification.channel == Channel.TELEGRAM.value:
                await self._forget_chat(session, notification, job.address)

        self._log(notification, result)

    async def _forget_chat(
        self, session: AsyncSession, notification: NotificationRecord, address: str | None
    ) -> None:
        """The user blocked the bot: stop offering Telegram until they link again."""
        if address is None or not address.lstrip("-").isdigit():
            return
        await RecipientRepository(session).forget_telegram_chat(
            notification.user_id, chat_id=int(address)
        )
        _logger.info("telegram chat unlinked after delivery", user_id=str(notification.user_id))

    def _pause_after(self, attempts: int, result: DeliveryResult) -> timedelta:
        """Doubling pauses, or longer if the provider asked for longer."""
        pause: timedelta = self._retry_delay * int(2 ** (attempts - 1))
        if result.retry_after_seconds is not None:
            pause = max(pause, timedelta(seconds=result.retry_after_seconds))
        return pause

    def _log(self, notification: NotificationRecord, result: DeliveryResult) -> None:
        fields: dict[str, object] = {
            "notification_id": str(notification.id),
            "user_id": str(notification.user_id),
            "channel": notification.channel,
            "template": notification.template,
            "attempt": notification.attempts,
        }
        if result.outcome is DeliveryOutcome.SENT:
            _logger.info("notification sent", **fields)
        else:
            _logger.warning(
                "notification not delivered",
                outcome=result.outcome.value,
                detail=result.detail,
                **fields,
            )
