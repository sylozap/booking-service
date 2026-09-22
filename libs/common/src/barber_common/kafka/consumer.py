"""Consumer runner that handles every event the same way.

The offset is committed only after the transaction of the handler commits; a
message replayed after a crash is dropped by ``processed_events``.

What the runner does around a handler:

* restores ``correlation_id`` and the trace context from the message headers;
* routes by ``event_type``, logging and committing unknown types;
* opens one transaction, claims the event for the consumer group, calls the
  handler, commits;
* sends unparseable messages to the dead letter topic at once, and failed
  handlers there after the retries.

A handler works in the session of that transaction and neither commits nor
talks to the broker.
"""

from __future__ import annotations

import asyncio
import json
import random
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Self

from aiokafka import AIOKafkaConsumer, ConsumerRecord, TopicPartition
from opentelemetry import trace
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from barber_common.context import bind_context
from barber_common.db.session import unit_of_work
from barber_common.events.envelope import JsonEnvelope
from barber_common.kafka.dedup import ProcessedEventRepository
from barber_common.kafka.dlq import DeadLetter, DeadLetterPublisher
from barber_common.logging import get_logger
from barber_common.metrics import counter, gauge
from barber_common.tracing import extract_trace_context

__all__ = [
    "EventConsumer",
    "EventHandler",
    "ProcessingResult",
    "RetryPolicy",
]

# A handler gets the transaction and the event, and returns nothing: whatever
# it produces -- a row, an outbox record -- belongs to that transaction.
EventHandler = Callable[[AsyncSession, JsonEnvelope], Awaitable[None]]

_logger = get_logger(__name__)
_tracer = trace.get_tracer(__name__)

EVENTS_PROCESSED = counter(
    "events_processed_total",
    "Events taken off a topic, by what happened to them",
    labelnames=("topic", "event_type", "result"),
)

# Consumer lag per partition; the number of partitions per topic is fixed, so
# the cardinality is bounded.
CONSUMER_LAG = gauge(
    "kafka_consumer_lag",
    "Messages between the committed position of the group and the end of the partition",
    labelnames=("topic", "partition", "group"),
)


class ProcessingResult(StrEnum):
    """What happened to one message. Also the label of the counter."""

    OK = "ok"
    DUPLICATE = "duplicate"
    SKIPPED = "skipped"
    DEAD_LETTERED = "dead_lettered"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Exponential backoff with jitter between the attempts on one message.

    Separate from the HTTP retry policy: it retries a transaction and is bounded
    by how long a partition may be held.
    """

    attempts: int = 3
    initial_delay_seconds: float = 0.2
    multiplier: float = 2.0
    max_delay_seconds: float = 2.0
    jitter_ratio: float = 0.2

    def delay_for(self, attempt: int) -> float:
        """Delay before the given attempt, counted from 1."""
        delay = min(
            self.initial_delay_seconds * self.multiplier ** (attempt - 1),
            self.max_delay_seconds,
        )
        # Not a security decision, so the fast generator is the right one.
        jitter = delay * self.jitter_ratio * random.random()  # noqa: S311
        return delay + jitter


class InvalidMessage(Exception):
    """The bytes on the topic are not an envelope of this platform."""


class EventConsumer:
    """Runner of the consumers of one service.

    One instance owns one consumer group and the handlers of the event types
    that group cares about::

        consumer = EventConsumer(
            topics=["catalog.masters.v1"],
            group_id="booking.masters",
            bootstrap_servers=settings.kafka_bootstrap_servers,
            session_factory=database.session_factory,
            dead_letters=publisher,
        )
        consumer.register("master.deactivated", deactivate_master)
    """

    def __init__(
        self,
        *,
        topics: Sequence[str],
        group_id: str,
        session_factory: async_sessionmaker[AsyncSession],
        dead_letters: DeadLetterPublisher,
        bootstrap_servers: str = "",
        handlers: Mapping[str, EventHandler] | None = None,
        retry_policy: RetryPolicy | None = None,
        message_timeout_seconds: float = 30.0,
        poll_timeout_ms: int = 1000,
        client: AIOKafkaConsumer | None = None,
    ) -> None:
        self._group_id = group_id
        self._session_factory = session_factory
        self._dead_letters = dead_letters
        self._handlers: dict[str, EventHandler] = dict(handlers or {})
        self._retry_policy = retry_policy or RetryPolicy()
        # One message may not hold a partition indefinitely: retries included,
        # a single unreachable dependency must not stop everything behind it.
        self._message_timeout_seconds = message_timeout_seconds
        self._poll_timeout_ms = poll_timeout_ms
        self._client = client or AIOKafkaConsumer(
            *topics,
            bootstrap_servers=bootstrap_servers,
            group_id=group_id,
            # The whole point: the offset moves when the transaction is
            # committed, not when the message is read.
            enable_auto_commit=False,
            auto_offset_reset="earliest",
        )
        self._is_started = False

    def register(self, event_type: str, handler: EventHandler) -> None:
        """Bind a handler to an event type. A second binding is a mistake."""
        if event_type in self._handlers:
            raise ValueError(f"event type {event_type!r} already has a handler")
        self._handlers[event_type] = handler

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback_: TracebackType | None,
    ) -> None:
        await self.stop()

    async def start(self) -> None:
        if self._is_started:
            return
        await self._client.start()
        self._is_started = True
        _logger.info("consumer started", group_id=self._group_id)

    async def stop(self) -> None:
        """Leave the group cleanly, keeping the offsets already committed."""
        if not self._is_started:
            return
        await self._client.stop()
        self._is_started = False
        _logger.info("consumer stopped", group_id=self._group_id)

    async def run_once(self) -> int:
        """Read one batch, handle it, commit as it goes. Returns the count.

        Joins the group on the first pass rather than at startup, so a broker
        that is down delays consumption instead of stopping the service.
        """
        await self.start()
        batches = await self._client.getmany(timeout_ms=self._poll_timeout_ms)
        handled = 0
        for partition, records in batches.items():
            for record in records:
                await self.handle(record)
                await self._commit(partition, record.offset)
                handled += 1
        await self._report_lag()
        return handled

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Keep consuming until asked to stop.

        Started from the lifespan of the service, so a SIGTERM stops it between
        messages -- after an offset commit, never in the middle of a handler.
        """
        while not stop.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failure of the loop itself: the broker went away, or the
                # commit was rejected after a rebalance. The messages stay
                # uncommitted and are read again by whoever owns the partition.
                _logger.exception("consumer loop failed", group_id=self._group_id)
                await asyncio.sleep(1.0)

    @asynccontextmanager
    async def run_in_background(self) -> AsyncIterator[None]:
        """Run the consumer for as long as the block lasts.

        The task is held in a local variable rather than created and forgotten:
        a task nobody references can be collected mid-flight and its exception
        never surfaces.
        """
        stop = asyncio.Event()
        task = asyncio.create_task(self.run_forever(stop), name=f"consumer-{self._group_id}")
        try:
            yield
        finally:
            stop.set()
            await task

    async def handle(self, record: ConsumerRecord[bytes, bytes]) -> ProcessingResult:
        """Handle one message. Never raises: every outcome is a result.

        Raising here would leave the offset uncommitted and the partition
        stuck on the same message forever, which is exactly what the dead
        letter topic exists to prevent.
        """
        headers = dict(record.headers or ())
        carrier = {name: value.decode("utf-8", errors="replace") for name, value in headers.items()}
        correlation_id = carrier.get("correlation_id")

        with (
            bind_context(correlation_id=correlation_id),
            _tracer.start_as_current_span(
                f"consume {record.topic}",
                context=extract_trace_context(carrier),
                kind=trace.SpanKind.CONSUMER,
                attributes={
                    "messaging.system": "kafka",
                    "messaging.source.name": record.topic,
                    "messaging.kafka.consumer.group": self._group_id,
                },
            ),
        ):
            return await self._dispatch(record)

    async def _dispatch(self, record: ConsumerRecord[bytes, bytes]) -> ProcessingResult:
        try:
            envelope = _parse(record.value)
        except InvalidMessage as error:
            await self._dead_letter(record, reason="invalid_message", error=error, attempts=0)
            self._count(record.topic, "unparseable", ProcessingResult.DEAD_LETTERED)
            return ProcessingResult.DEAD_LETTERED

        handler = self._handlers.get(envelope.event_type)
        if handler is None:
            # Not an error: a producer of a newer version publishes types this
            # service has never heard of, and the tolerant reader rule says it
            # commits and moves on.
            _logger.info(
                "event type has no handler in this group",
                event_type=envelope.event_type,
                topic=record.topic,
                group_id=self._group_id,
            )
            self._count(record.topic, envelope.event_type, ProcessingResult.SKIPPED)
            return ProcessingResult.SKIPPED

        return await self._run_handler(record, envelope, handler)

    async def _run_handler(
        self,
        record: ConsumerRecord[bytes, bytes],
        envelope: JsonEnvelope,
        handler: EventHandler,
    ) -> ProcessingResult:
        """Try the handler, retry a temporary failure, then give up loudly."""
        attempts = 0
        last_error: Exception | None = None

        try:
            async with asyncio.timeout(self._message_timeout_seconds):
                for attempt in range(1, self._retry_policy.attempts + 1):
                    attempts = attempt
                    try:
                        return await self._apply(envelope, handler, record.topic)
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        last_error = error
                        _logger.warning(
                            "event handling failed",
                            event_type=envelope.event_type,
                            event_id=str(envelope.event_id),
                            topic=record.topic,
                            attempt=attempt,
                            error=type(error).__name__,
                        )
                        if attempt < self._retry_policy.attempts:
                            await asyncio.sleep(self._retry_policy.delay_for(attempt))
        except TimeoutError as error:
            last_error = error

        await self._dead_letter(
            record,
            reason="handler_failed",
            error=last_error,
            attempts=attempts,
        )
        self._count(record.topic, envelope.event_type, ProcessingResult.DEAD_LETTERED)
        return ProcessingResult.DEAD_LETTERED

    async def _apply(
        self,
        envelope: JsonEnvelope,
        handler: EventHandler,
        topic: str,
    ) -> ProcessingResult:
        """The transaction: claim the event, do the work, commit both.

        The claim and the effect are one write. A duplicate loses the claim and
        leaves without touching anything, which is what makes redelivery free.
        """
        async with unit_of_work(self._session_factory) as session:
            processed = ProcessedEventRepository(session, consumer_group=self._group_id)
            if not await processed.claim(envelope.event_id):
                _logger.info(
                    "event already processed by this group",
                    event_type=envelope.event_type,
                    event_id=str(envelope.event_id),
                    group_id=self._group_id,
                )
                self._count(topic, envelope.event_type, ProcessingResult.DUPLICATE)
                return ProcessingResult.DUPLICATE

            await handler(session, envelope)

        _logger.info(
            "event processed",
            event_type=envelope.event_type,
            event_id=str(envelope.event_id),
            topic=topic,
        )
        self._count(topic, envelope.event_type, ProcessingResult.OK)
        return ProcessingResult.OK

    async def _dead_letter(
        self,
        record: ConsumerRecord[bytes, bytes],
        *,
        reason: str,
        error: Exception | None,
        attempts: int,
    ) -> None:
        await self._dead_letters.publish(
            DeadLetter(
                original_topic=record.topic,
                reason=reason,
                detail=f"{type(error).__name__}: {error}" if error else reason,
                attempts=attempts,
                value=record.value or b"",
                key=record.key,
                headers=list(record.headers or ()),
                traceback=_format_traceback(error),
            )
        )

    async def _commit(self, partition: TopicPartition, offset: int) -> None:
        """Move the group past this message. Always after the transaction."""
        await self._client.commit({partition: offset + 1})

    async def _report_lag(self) -> None:
        """Publish how far this group is behind the end of each partition."""
        for partition in self._client.assignment():
            end = self._client.highwater(partition)
            if end is None:
                continue
            position = await self._client.position(partition)
            CONSUMER_LAG.labels(
                topic=partition.topic,
                partition=str(partition.partition),
                group=self._group_id,
            ).set(max(0, end - position))

    def _count(self, topic: str, event_type: str, result: ProcessingResult) -> None:
        EVENTS_PROCESSED.labels(topic=topic, event_type=event_type, result=result.value).inc()


def _parse(value: bytes | None) -> JsonEnvelope:
    """Read the envelope, or say that these bytes are not one.

    Both failures are final. Bytes that are not JSON and JSON that is not an
    envelope will be exactly as wrong on the next attempt.
    """
    if not value:
        raise InvalidMessage("message has an empty body")
    try:
        document = json.loads(value)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise InvalidMessage(f"body is not JSON: {error}") from error

    try:
        return JsonEnvelope.model_validate(document)
    except ValidationError as error:
        raise InvalidMessage(f"body is not an event envelope: {error.error_count()} errors") from (
            error
        )


def _format_traceback(error: Exception | None) -> str | None:
    if error is None:
        return None
    return "".join(traceback.format_exception(type(error), error, error.__traceback__))
