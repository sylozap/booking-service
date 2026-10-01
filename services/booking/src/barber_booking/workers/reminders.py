"""The reminder scheduler: ``reminder.due`` once a booking's ``reminder_at`` comes.

Kafka cannot hold a message back until a moment, and a consumer that sleeps
holds its partition, so the schedule lives in the database (ADR-0011). Every
minute a pass takes the bookings whose reminder is due and, in one transaction,
writes ``reminder.due`` to the outbox and stamps ``reminder_sent_at``.

That one transaction is the whole guarantee. A pod that dies before the commit
leaves the reminders unstamped and the next pass sends them; one that dies
after has sent them through the outbox already. ``SKIP LOCKED`` keeps two
replicas from taking the same booking. A cancelled booking is not a candidate,
and a moved one has a fresh ``reminder_at`` and no stamp.

A reminder that comes after its visit has begun -- the scheduler was down for
hours -- tells the client nothing. It is stamped without an event and logged.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from barber_booking.metrics import REMINDER_SCHEDULER_LAST_RUN
from barber_booking.repositories.bookings import BookingRepository, DueReminder
from barber_booking.services.clock import Clock, utc_now
from barber_common.db.session import unit_of_work
from barber_common.events.bookings import (
    BOOKING_AGGREGATE_TYPE,
    REMINDERS_TOPIC,
    ReminderDue,
    ReminderEventType,
)
from barber_common.logging import get_logger
from barber_common.outbox import OutboxRepository

__all__ = ["ReminderScheduler"]

_logger = get_logger(__name__)


class ReminderScheduler:
    """Publishes the reminders whose time has come."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock = utc_now,
        batch_size: int = 100,
        interval_seconds: float = 60.0,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._batch_size = batch_size
        self._interval_seconds = interval_seconds

    async def run_once(self) -> int:
        """One batch. Returns how many bookings it dealt with, sent or skipped."""
        now = self._clock()
        async with unit_of_work(self._session_factory) as session:
            bookings = BookingRepository(session)
            due = await bookings.claim_due_reminders(now=now, limit=self._batch_size)
            ahead = [reminder for reminder in due if not reminder.booking.has_started(now)]
            if ahead:
                await OutboxRepository(session).add_batch(
                    topic=REMINDERS_TOPIC,
                    aggregate_type=BOOKING_AGGREGATE_TYPE,
                    event_type=ReminderEventType.DUE,
                    events=[(reminder.booking.id, _payload(reminder)) for reminder in ahead],
                )
            await bookings.mark_reminders_sent([reminder.booking.id for reminder in due], at=now)

        # After the commit: a pass that rolled back has not run, as far as the
        # alert on a silent scheduler is concerned.
        REMINDER_SCHEDULER_LAST_RUN.set(now.timestamp())
        self._log(ahead, skipped=[reminder for reminder in due if reminder not in ahead])
        return len(due)

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Keep scheduling until asked to stop.

        A full batch is followed by another at once; otherwise the next pass
        comes a minute later. A failed pass is logged and the next one retries:
        nothing it held was committed.
        """
        _logger.info("reminder scheduler started", interval_seconds=self._interval_seconds)
        while not stop.is_set():
            try:
                taken = await self.run_once()
            except Exception:
                _logger.exception("reminder scheduler pass failed")
                taken = 0
            if taken >= self._batch_size:
                continue
            try:
                async with asyncio.timeout(self._interval_seconds):
                    await stop.wait()
            except TimeoutError:
                continue
        _logger.info("reminder scheduler stopped")

    @asynccontextmanager
    async def run_in_background(self) -> AsyncIterator[None]:
        """Run the scheduler for as long as the block lasts.

        The task is held in a local variable rather than created and forgotten:
        a task nobody references can be collected mid-flight and its exception
        never surfaces.
        """
        stop = asyncio.Event()
        task = asyncio.create_task(self.run_forever(stop), name="reminder-scheduler")
        try:
            yield
        finally:
            stop.set()
            await task

    def _log(self, published: Sequence[DueReminder], *, skipped: Sequence[DueReminder]) -> None:
        for reminder in skipped:
            _logger.info(
                "reminder skipped: the visit has begun",
                booking_id=str(reminder.booking.id),
            )
        if published:
            _logger.info("reminders published", count=len(published))


def _payload(reminder: DueReminder) -> ReminderDue:
    booking = reminder.booking
    return ReminderDue(
        booking_id=booking.id,
        salon_id=booking.salon_id,
        master_id=booking.master_id,
        client_user_id=booking.client_user_id,
        service_name=booking.service.name,
        start_at=booking.start_at,
        end_at=booking.end_at,
        # Claimed only with a reminder_at, so the lead is always there.
        hours_before=booking.reminder_lead_hours or 0,
        timezone=reminder.timezone,
    )
