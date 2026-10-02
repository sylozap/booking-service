"""Worker that publishes outbox rows to Kafka.

Each pass claims a batch with ``FOR UPDATE SKIP LOCKED``, publishes it and marks
it published in one transaction. Publishing inside the transaction is
deliberate: the row lock stops another replica from sending the same event.
Delivery is at-least-once, and a republished event keeps its ``event_id``.

The relay publishes in the context stored with each row, not in its own: see
:mod:`barber_common.tracing` for why its span is linked rather than a child.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from barber_common.db.session import unit_of_work
from barber_common.events.envelope import EventEnvelope
from barber_common.kafka.producer import EventProducer
from barber_common.logging import get_logger
from barber_common.metrics import gauge
from barber_common.outbox.models import OutboxMessage
from barber_common.outbox.repository import OutboxRepository

__all__ = ["OutboxRelay"]

_logger = get_logger(__name__)

# Both answer the same question -- have the database and the broker drifted
# apart -- from two sides: how many events are waiting and how old the oldest
# one is. A count alone hides a single stuck row; an age alone hides a backlog.
OUTBOX_PENDING = gauge(
    "outbox_pending_messages",
    "Events written to the outbox and not yet published",
)
OUTBOX_LAG = gauge(
    "outbox_publish_lag_seconds",
    "Age of the oldest event still waiting in the outbox",
)


class OutboxRelay:
    """Publishes what the scenarios wrote to the outbox."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        producer: EventProducer,
        batch_size: int = 100,
        idle_interval_seconds: float = 0.5,
    ) -> None:
        self._session_factory = session_factory
        self._producer = producer
        self._batch_size = batch_size
        self._idle_interval_seconds = idle_interval_seconds

    async def run_once(self) -> int:
        """Publish one batch. Returns how many events went out."""
        async with unit_of_work(self._session_factory) as session:
            repository = OutboxRepository(session)
            messages = await repository.claim_batch(limit=self._batch_size)
            if not messages:
                await self._report(repository)
                return 0

            # Connected here rather than at startup, so a broker that is down
            # delays events instead of stopping the service. The call is
            # idempotent.
            await self._producer.start()

            for message in messages:
                await self._publish(message)

            await repository.mark_published([message.id for message in messages])
            await session.flush()
            await self._report(repository)

        return len(messages)

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Keep publishing until asked to stop.

        Started from the lifespan of the service, so that a SIGTERM stops it
        between batches rather than in the middle of one.
        """
        _logger.info("outbox relay started", batch_size=self._batch_size)
        while not stop.is_set():
            published = await self._pass()
            if published:
                continue
            # Nothing to do: wait, but wake up immediately when asked to stop.
            try:
                async with asyncio.timeout(self._idle_interval_seconds):
                    await stop.wait()
            except TimeoutError:
                continue
        _logger.info("outbox relay stopped")

    @asynccontextmanager
    async def run_in_background(self) -> AsyncIterator[None]:
        """Run the relay for as long as the block lasts.

        Used from the lifespan of a service. The task is held in a local
        variable rather than created and forgotten: a task nobody references
        can be collected mid-flight and its exception never surfaces.
        """
        stop = asyncio.Event()
        task = asyncio.create_task(self.run_forever(stop), name="outbox-relay")
        try:
            yield
        finally:
            stop.set()
            await task

    async def _pass(self) -> int:
        """One pass that survives a broker or database outage.

        A failure here is not fatal: the rows stay unpublished, the gauges grow,
        and the next pass sends them. That is the behaviour the failure drill
        expects -- stop Kafka, watch the outbox fill, start it, watch it empty.
        """
        try:
            return await self.run_once()
        except Exception:
            _logger.exception("outbox relay pass failed")
            await self._report_after_failure()
            await asyncio.sleep(self._idle_interval_seconds)
            return 0

    async def _report_after_failure(self) -> None:
        """Measure the outbox in a transaction of its own.

        The failed pass rolled back before it got to measuring, and the gauges
        would keep the last good value -- usually zero -- for as long as the
        broker stays down: the alert on a stuck outbox would never fire. With
        the database down too there is nothing to measure, and that outage
        shows elsewhere.
        """
        try:
            async with unit_of_work(self._session_factory) as session:
                await self._report(OutboxRepository(session))
        except Exception:
            _logger.warning("outbox gauges not refreshed after a failed pass", exc_info=True)

    async def _publish(self, message: OutboxMessage) -> None:
        envelope: EventEnvelope[dict[str, object]] = EventEnvelope(
            event_id=message.id,
            event_type=message.event_type,
            event_version=message.event_version,
            occurred_at=message.created_at,
            correlation_id=message.correlation_id,
            causation_id=message.causation_id,
            producer=self._producer.producer_name,
            payload=message.payload,
        )
        await self._producer.publish(
            topic=message.topic,
            aggregate_id=message.aggregate_id,
            envelope=envelope,
            traceparent=message.traceparent,
        )

    async def _report(self, repository: OutboxRepository) -> None:
        OUTBOX_PENDING.set(await repository.count_pending())
        OUTBOX_LAG.set(await repository.oldest_pending_age_seconds())
