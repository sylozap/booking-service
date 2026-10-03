"""The consumer against a real broker and a real database.

What is being proved: a redelivery has no second effect, a message that cannot
be handled leaves the partition instead of stopping it, and the offset moves
only after the transaction is committed.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from barber_common.context import get_correlation_id
from barber_common.events.envelope import JsonEnvelope, build_envelope
from barber_common.kafka.consumer import EventConsumer, RetryPolicy
from barber_common.kafka.dlq import DeadLetterPublisher, dlq_topic_of
from barber_common.kafka.producer import EventProducer
from barber_common.metrics import REGISTRY
from barber_common.outbox.models import OutboxMessage
from barber_common.outbox.repository import OutboxRepository
from barber_common.testing.fixtures import read_events

pytestmark = pytest.mark.integration

EFFECT_EVENT_TYPE = "test.recorded"
# Fast, because these tests are about how many attempts happen, not how long
# the pauses between them are.
FAST_RETRIES = RetryPolicy(attempts=3, initial_delay_seconds=0.01, max_delay_seconds=0.02)


class Recorded(BaseModel):
    value: str


@pytest.fixture
def session_factory(
    concurrent_session_factory: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    """Real connections: the handler commits, and the test reads it back."""
    return concurrent_session_factory


@pytest.fixture
def topic() -> str:
    """A topic of its own per test.

    The broker lives for the whole session, so a shared topic would let one
    test read the message of another and pass for the wrong reason.
    """
    return f"test.events.v1.{uuid4().hex[:12]}"


@pytest.fixture
def group_id() -> str:
    return f"test-group-{uuid4().hex[:12]}"


@pytest.fixture
async def producer(kafka_bootstrap: str) -> AsyncIterator[EventProducer]:
    async with EventProducer(bootstrap_servers=kafka_bootstrap, service_name="test") as producer:
        yield producer


@pytest.fixture
async def dead_letters(kafka_bootstrap: str) -> AsyncIterator[DeadLetterPublisher]:
    async with DeadLetterPublisher(bootstrap_servers=kafka_bootstrap) as publisher:
        yield publisher


class Handler:
    """A handler that records what it was asked to do.

    The effect is a row in the outbox: a real table of the shared schema, so
    the test needs no table of its own and no ``create_all``.
    """

    def __init__(self, *, fail_always: bool = False) -> None:
        self.calls = 0
        self.correlation_ids: list[str | None] = []
        self._fail_always = fail_always

    async def __call__(self, session: AsyncSession, envelope: JsonEnvelope) -> None:
        self.calls += 1
        self.correlation_ids.append(get_correlation_id())
        if self._fail_always:
            raise RuntimeError("the database is unreachable")

        await OutboxRepository(session).add(
            topic="test.effects.v1",
            aggregate_type="tests",
            aggregate_id=uuid4(),
            event_type=EFFECT_EVENT_TYPE,
            payload=Recorded(value=str(envelope.payload.get("value"))),
        )


def build_consumer(
    *,
    topic: str,
    group_id: str,
    kafka_bootstrap: str,
    session_factory: async_sessionmaker[AsyncSession],
    dead_letters: DeadLetterPublisher,
    handler: Handler | None,
) -> tuple[EventConsumer, AIOKafkaConsumer[bytes, bytes]]:
    """Build the runner over a client the test keeps a reference to.

    The reference is what lets the assertions ask the broker where the group
    stands, which is the point of half of these tests.
    """
    client: AIOKafkaConsumer[bytes, bytes] = AIOKafkaConsumer(
        topic,
        bootstrap_servers=kafka_bootstrap,
        group_id=group_id,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
    )
    consumer = EventConsumer(
        topics=[topic],
        group_id=group_id,
        session_factory=session_factory,
        dead_letters=dead_letters,
        handlers={"test.happened": handler} if handler is not None else {},
        retry_policy=FAST_RETRIES,
        client=client,
    )
    return consumer, client


async def drain(consumer: EventConsumer, *, expected: int, timeout_seconds: float = 60.0) -> None:
    """Consume until the expected number of messages has been handled."""
    handled = 0
    async with asyncio.timeout(timeout_seconds):
        while handled < expected:
            handled += await consumer.run_once()


async def publish(producer: EventProducer, topic: str, envelope: JsonEnvelope) -> None:
    await producer.publish(topic=topic, aggregate_id=uuid4(), envelope=envelope)


def event(value: str = "one", correlation_id: str | None = None) -> JsonEnvelope:
    return build_envelope(
        event_type="test.happened",
        payload={"value": value},
        producer="test@0.1.0",
        correlation_id=correlation_id,
    )


async def count_effects(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        result = await connection.execute(
            select(func.count())
            .select_from(OutboxMessage)
            .where(OutboxMessage.event_type == EFFECT_EVENT_TYPE)
        )
        return int(result.scalar_one())


async def read_dead_letter(kafka_bootstrap: str, topic: str) -> dict[str, object]:
    records = await read_events(bootstrap_servers=kafka_bootstrap, topic=dlq_topic_of(topic))
    document = json.loads(records[0].value or b"{}")
    assert isinstance(document, dict)
    return document


def retries_of(topic: str) -> float | None:
    return REGISTRY.get_sample_value(
        "event_retries_total", {"topic": topic, "event_type": "test.happened"}
    )


async def test_the_dead_letter_counter_of_each_topic_starts_at_zero(
    topic: str,
    group_id: str,
    kafka_bootstrap: str,
    session_factory: async_sessionmaker[AsyncSession],
    dead_letters: DeadLetterPublisher,
) -> None:
    build_consumer(
        topic=topic,
        group_id=group_id,
        kafka_bootstrap=kafka_bootstrap,
        session_factory=session_factory,
        dead_letters=dead_letters,
        handler=Handler(),
    )

    # Before a single message: increase() over a series born at one sees no
    # growth, and the alert would miss the first dead letter.
    for reason in ("invalid_message", "handler_failed"):
        sample = REGISTRY.get_sample_value("dlq_messages_total", {"topic": topic, "reason": reason})
        assert sample == 0.0


async def test_a_redelivered_event_has_no_second_effect(
    topic: str,
    group_id: str,
    kafka_bootstrap: str,
    session_factory: async_sessionmaker[AsyncSession],
    dead_letters: DeadLetterPublisher,
    producer: EventProducer,
    engine: AsyncEngine,
) -> None:
    handler = Handler()
    consumer, _ = build_consumer(
        topic=topic,
        group_id=group_id,
        kafka_bootstrap=kafka_bootstrap,
        session_factory=session_factory,
        dead_letters=dead_letters,
        handler=handler,
    )
    envelope = event()
    await publish(producer, topic, envelope)
    await publish(producer, topic, envelope)

    async with consumer:
        await drain(consumer, expected=2)

    assert handler.calls == 1
    assert await count_effects(engine) == 1
    # Nothing failed, so nothing was tried again.
    assert retries_of(topic) is None


async def test_an_invalid_message_goes_to_the_dead_letter_topic_without_retries(
    topic: str,
    group_id: str,
    kafka_bootstrap: str,
    session_factory: async_sessionmaker[AsyncSession],
    dead_letters: DeadLetterPublisher,
) -> None:
    handler = Handler()
    consumer, _ = build_consumer(
        topic=topic,
        group_id=group_id,
        kafka_bootstrap=kafka_bootstrap,
        session_factory=session_factory,
        dead_letters=dead_letters,
        handler=handler,
    )
    raw = AIOKafkaProducer(bootstrap_servers=kafka_bootstrap)
    await raw.start()
    try:
        await raw.send_and_wait(topic, value=b"{ this is not json")
    finally:
        await raw.stop()

    async with consumer:
        await drain(consumer, expected=1)
    letter = await read_dead_letter(kafka_bootstrap, topic)

    assert handler.calls == 0
    assert letter["dlq_reason"] == "invalid_message"
    assert letter["dlq_attempts"] == 0
    assert letter["dlq_original_topic"] == topic
    # The bytes themselves are kept: a review with no payload reviews nothing.
    assert letter["dlq_original_value"] == "{ this is not json"


async def test_a_temporary_failure_is_retried_three_times_and_then_dead_lettered(
    topic: str,
    group_id: str,
    kafka_bootstrap: str,
    session_factory: async_sessionmaker[AsyncSession],
    dead_letters: DeadLetterPublisher,
    producer: EventProducer,
    engine: AsyncEngine,
) -> None:
    handler = Handler(fail_always=True)
    consumer, _ = build_consumer(
        topic=topic,
        group_id=group_id,
        kafka_bootstrap=kafka_bootstrap,
        session_factory=session_factory,
        dead_letters=dead_letters,
        handler=handler,
    )
    await publish(producer, topic, event())

    async with consumer:
        await drain(consumer, expected=1)
    letter = await read_dead_letter(kafka_bootstrap, topic)

    assert handler.calls == FAST_RETRIES.attempts
    # The first attempt is not a retry.
    assert retries_of(topic) == FAST_RETRIES.attempts - 1
    assert letter["dlq_reason"] == "handler_failed"
    assert letter["dlq_attempts"] == FAST_RETRIES.attempts
    assert "RuntimeError" in str(letter["dlq_traceback"])
    # The failed attempts rolled back, so the effect of none of them survived.
    assert await count_effects(engine) == 0


async def test_the_offset_moves_past_a_message_that_was_dead_lettered(
    topic: str,
    group_id: str,
    kafka_bootstrap: str,
    session_factory: async_sessionmaker[AsyncSession],
    dead_letters: DeadLetterPublisher,
    producer: EventProducer,
) -> None:
    consumer, client = build_consumer(
        topic=topic,
        group_id=group_id,
        kafka_bootstrap=kafka_bootstrap,
        session_factory=session_factory,
        dead_letters=dead_letters,
        handler=Handler(fail_always=True),
    )
    await publish(producer, topic, event())

    async with consumer:
        await drain(consumer, expected=1)
        committed = await client.committed(TopicPartition(topic, 0))

    # A poison message that keeps its offset stops the partition, and with it
    # every healthy message behind it.
    assert committed == 1


async def test_the_offset_moves_after_a_successful_transaction(
    topic: str,
    group_id: str,
    kafka_bootstrap: str,
    session_factory: async_sessionmaker[AsyncSession],
    dead_letters: DeadLetterPublisher,
    producer: EventProducer,
) -> None:
    consumer, client = build_consumer(
        topic=topic,
        group_id=group_id,
        kafka_bootstrap=kafka_bootstrap,
        session_factory=session_factory,
        dead_letters=dead_letters,
        handler=Handler(),
    )
    await publish(producer, topic, event())

    async with consumer:
        await drain(consumer, expected=1)
        committed = await client.committed(TopicPartition(topic, 0))

    assert committed == 1


async def test_an_unknown_event_type_is_committed_instead_of_stopping_the_partition(
    topic: str,
    group_id: str,
    kafka_bootstrap: str,
    session_factory: async_sessionmaker[AsyncSession],
    dead_letters: DeadLetterPublisher,
    producer: EventProducer,
    engine: AsyncEngine,
) -> None:
    consumer, client = build_consumer(
        topic=topic,
        group_id=group_id,
        kafka_bootstrap=kafka_bootstrap,
        session_factory=session_factory,
        dead_letters=dead_letters,
        handler=None,
    )
    await publish(producer, topic, event())

    async with consumer:
        await drain(consumer, expected=1)
        committed = await client.committed(TopicPartition(topic, 0))

    assert committed == 1
    assert await count_effects(engine) == 0


async def test_the_correlation_id_of_the_producer_reaches_the_handler(
    topic: str,
    group_id: str,
    kafka_bootstrap: str,
    session_factory: async_sessionmaker[AsyncSession],
    dead_letters: DeadLetterPublisher,
    producer: EventProducer,
) -> None:
    handler = Handler()
    consumer, _ = build_consumer(
        topic=topic,
        group_id=group_id,
        kafka_bootstrap=kafka_bootstrap,
        session_factory=session_factory,
        dead_letters=dead_letters,
        handler=handler,
    )
    await publish(producer, topic, event(correlation_id="c-8f2a"))

    async with consumer:
        await drain(consumer, expected=1)

    assert handler.correlation_ids == ["c-8f2a"]


class StopOnFirstCall(Handler):
    """A handler during whose first message the shutdown begins."""

    def __init__(self, stop: asyncio.Event) -> None:
        super().__init__()
        self._stop = stop

    async def __call__(self, session: AsyncSession, envelope: JsonEnvelope) -> None:
        await super().__call__(session, envelope)
        self._stop.set()


async def test_a_shutdown_mid_batch_commits_the_message_in_hand_and_leaves_the_rest(
    topic: str,
    group_id: str,
    kafka_bootstrap: str,
    session_factory: async_sessionmaker[AsyncSession],
    dead_letters: DeadLetterPublisher,
    producer: EventProducer,
    engine: AsyncEngine,
) -> None:
    stop = asyncio.Event()
    stopping = StopOnFirstCall(stop)
    consumer, client = build_consumer(
        topic=topic,
        group_id=group_id,
        kafka_bootstrap=kafka_bootstrap,
        session_factory=session_factory,
        dead_letters=dead_letters,
        handler=stopping,
    )
    # One aggregate, so one partition and one order: the first message
    # handled is the first one written.
    aggregate_id = uuid4()
    for value in ("one", "two", "three", "four", "five"):
        await producer.publish(topic=topic, aggregate_id=aggregate_id, envelope=event(value))

    async with consumer:
        handled = 0
        async with asyncio.timeout(60):
            while handled == 0:
                handled = await consumer.run_once(stop)
        [partition] = client.assignment()
        committed = await client.committed(partition)

    assert handled == 1
    assert stopping.calls == 1
    assert committed == 1

    # The next owner of the partition picks up exactly where the first left.
    successor = Handler()
    next_consumer, _ = build_consumer(
        topic=topic,
        group_id=group_id,
        kafka_bootstrap=kafka_bootstrap,
        session_factory=session_factory,
        dead_letters=dead_letters,
        handler=successor,
    )
    async with next_consumer:
        await drain(next_consumer, expected=4)

    assert successor.calls == 4
    assert await count_effects(engine) == 5
