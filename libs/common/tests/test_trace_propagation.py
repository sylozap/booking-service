"""One trace from the write of an event to its handler, across outbox and broker.

The outbox stores the context the event was written in, the relay publishes in
it without becoming part of it, and the consumer continues it. The database
and the broker are real: the context travels through a column and a header,
and a fake of either would prove only that the fake passes it on.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from uuid import uuid4

import pytest
from aiokafka import ConsumerRecord
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from barber_common.db import unit_of_work
from barber_common.events.envelope import JsonEnvelope, build_envelope
from barber_common.kafka.consumer import EventConsumer
from barber_common.kafka.dlq import DeadLetterPublisher
from barber_common.kafka.producer import EventProducer
from barber_common.outbox.models import OutboxMessage
from barber_common.outbox.relay import OutboxRelay
from barber_common.outbox.repository import OutboxRepository
from barber_common.testing import read_events, recorded_spans
from barber_common.tracing import TRACEPARENT_HEADER, context_of, current_traceparent

pytestmark = pytest.mark.integration

EVENT_TYPE = "test.happened"
EFFECT_TOPIC = "test.effects.v1"


def tracer() -> trace.Tracer:
    """Taken per call: a tracer held by the module keeps the provider of the first test."""
    return trace.get_tracer(__name__)


class Happened(BaseModel):
    value: str


@pytest.fixture
def session_factory(
    concurrent_session_factory: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    """Real commits: the relay and the consumer read what the test wrote."""
    return concurrent_session_factory


@pytest.fixture
def topic() -> str:
    """A topic of its own per test, so no test reads the message of another."""
    return f"test.events.v1.{uuid4().hex[:12]}"


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    with recorded_spans() as exporter:
        yield exporter


@pytest.fixture
async def dead_letters(kafka_bootstrap: str) -> AsyncIterator[DeadLetterPublisher]:
    async with DeadLetterPublisher(bootstrap_servers=kafka_bootstrap) as publisher:
        yield publisher


class Handler:
    """Writes an event of its own, the way a cascade does."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, session: AsyncSession, envelope: JsonEnvelope) -> None:
        self.calls += 1
        await OutboxRepository(session).add(
            topic=EFFECT_TOPIC,
            aggregate_type="tests",
            aggregate_id=uuid4(),
            event_type="test.followed",
            payload=Happened(value="effect"),
        )


def build_consumer(
    *,
    topic: str,
    kafka_bootstrap: str,
    session_factory: async_sessionmaker[AsyncSession],
    dead_letters: DeadLetterPublisher,
    handler: Handler,
) -> EventConsumer:
    return EventConsumer(
        topics=[topic],
        group_id=f"test-group-{uuid4().hex[:12]}",
        bootstrap_servers=kafka_bootstrap,
        session_factory=session_factory,
        dead_letters=dead_letters,
        handlers={EVENT_TYPE: handler},
    )


def record_of(
    topic: str, *, traceparent: str | None
) -> tuple[ConsumerRecord[bytes, bytes], JsonEnvelope]:
    """A message as the broker hands it over, with or without a trace."""
    envelope = build_envelope(event_type=EVENT_TYPE, payload=Happened(value="x"), producer="t@1")
    value = envelope.model_dump_json().encode("utf-8")
    headers = [
        ("event_type", EVENT_TYPE.encode("utf-8")),
        ("event_id", str(envelope.event_id).encode("utf-8")),
    ]
    if traceparent is not None:
        headers.append((TRACEPARENT_HEADER, traceparent.encode("utf-8")))
    record: ConsumerRecord[bytes, bytes] = ConsumerRecord(
        topic=topic,
        partition=0,
        offset=0,
        timestamp=0,
        timestamp_type=0,
        key=b"aggregate",
        value=value,
        checksum=None,
        serialized_key_size=9,
        serialized_value_size=len(value),
        headers=headers,
    )
    return record, JsonEnvelope.model_validate_json(value)


def span_context_of(traceparent: str | None) -> trace.SpanContext:
    return trace.get_current_span(context_of(traceparent)).get_span_context()


def only_span(exporter: InMemorySpanExporter, name: str) -> ReadableSpan:
    found = [span for span in exporter.get_finished_spans() if span.name == name]
    assert len(found) == 1, f"expected one span {name!r}, got {len(found)}"
    return found[0]


def ids_of(span: ReadableSpan) -> tuple[int, int]:
    assert span.context is not None
    return span.context.trace_id, span.context.span_id


async def write_in_request(
    session_factory: async_sessionmaker[AsyncSession], topic: str
) -> tuple[trace.SpanContext, OutboxMessage]:
    """Write one event inside a span standing in for the request."""
    with tracer().start_as_current_span("request") as request:
        async with unit_of_work(session_factory) as session:
            message = await OutboxRepository(session).add(
                topic=topic,
                aggregate_type="tests",
                aggregate_id=uuid4(),
                event_type=EVENT_TYPE,
                payload=Happened(value="x"),
            )
    return request.get_span_context(), message


async def relay_once(session_factory: async_sessionmaker[AsyncSession], kafka: str) -> None:
    async with EventProducer(bootstrap_servers=kafka, service_name="test") as producer:
        await OutboxRelay(session_factory=session_factory, producer=producer).run_once()


async def effects(session_factory: async_sessionmaker[AsyncSession]) -> list[OutboxMessage]:
    async with session_factory() as session:
        statement = select(OutboxMessage).where(OutboxMessage.topic == EFFECT_TOPIC)
        return list((await session.execute(statement)).scalars().all())


async def test_outbox_stores_a_create_span_inside_the_trace_of_the_write(
    session_factory: async_sessionmaker[AsyncSession], topic: str, spans: InMemorySpanExporter
) -> None:
    request, message = await write_in_request(session_factory, topic)

    create = only_span(spans, f"create {topic}")
    stored = span_context_of(message.traceparent)

    assert (stored.trace_id, stored.span_id) == ids_of(create)
    assert create.parent is not None
    assert create.parent.span_id == request.span_id


async def test_outbox_stores_no_trace_while_tracing_is_off(
    session_factory: async_sessionmaker[AsyncSession], topic: str
) -> None:
    _, message = await write_in_request(session_factory, topic)

    assert message.traceparent is None


async def test_relay_sends_the_stored_context_in_the_headers(
    session_factory: async_sessionmaker[AsyncSession],
    kafka_bootstrap: str,
    topic: str,
    spans: InMemorySpanExporter,
) -> None:
    _, message = await write_in_request(session_factory, topic)

    await relay_once(session_factory, kafka_bootstrap)
    [record] = await read_events(bootstrap_servers=kafka_bootstrap, topic=topic)

    assert dict(record.headers)[TRACEPARENT_HEADER].decode() == message.traceparent


async def test_relay_publish_span_is_linked_to_the_write_not_its_child(
    session_factory: async_sessionmaker[AsyncSession],
    kafka_bootstrap: str,
    topic: str,
    spans: InMemorySpanExporter,
) -> None:
    request, _ = await write_in_request(session_factory, topic)

    await relay_once(session_factory, kafka_bootstrap)
    publish = only_span(spans, f"publish {topic}")
    create = only_span(spans, f"create {topic}")

    assert publish.parent is None
    assert ids_of(publish)[0] != request.trace_id
    assert [(link.context.trace_id, link.context.span_id) for link in publish.links] == [
        ids_of(create)
    ]


async def test_consumer_continues_the_trace_from_the_headers(
    session_factory: async_sessionmaker[AsyncSession],
    kafka_bootstrap: str,
    topic: str,
    dead_letters: DeadLetterPublisher,
    spans: InMemorySpanExporter,
) -> None:
    with tracer().start_as_current_span("upstream") as upstream:
        traceparent = current_traceparent()
    record, _ = record_of(topic, traceparent=traceparent)
    consumer = build_consumer(
        topic=topic,
        kafka_bootstrap=kafka_bootstrap,
        session_factory=session_factory,
        dead_letters=dead_letters,
        handler=Handler(),
    )

    await consumer.handle(record)
    consume = only_span(spans, f"consume {topic}")

    assert ids_of(consume)[0] == upstream.get_span_context().trace_id
    assert consume.parent is not None
    assert consume.parent.span_id == upstream.get_span_context().span_id


async def test_consumer_starts_a_trace_when_the_message_carries_none(
    session_factory: async_sessionmaker[AsyncSession],
    kafka_bootstrap: str,
    topic: str,
    dead_letters: DeadLetterPublisher,
    spans: InMemorySpanExporter,
) -> None:
    record, _ = record_of(topic, traceparent=None)
    consumer = build_consumer(
        topic=topic,
        kafka_bootstrap=kafka_bootstrap,
        session_factory=session_factory,
        dead_letters=dead_letters,
        handler=Handler(),
    )

    await consumer.handle(record)

    assert only_span(spans, f"consume {topic}").parent is None


async def test_event_written_by_a_handler_is_caused_by_the_handled_event(
    session_factory: async_sessionmaker[AsyncSession],
    kafka_bootstrap: str,
    topic: str,
    dead_letters: DeadLetterPublisher,
    spans: InMemorySpanExporter,
) -> None:
    with tracer().start_as_current_span("upstream") as upstream:
        traceparent = current_traceparent()
    record, envelope = record_of(topic, traceparent=traceparent)
    consumer = build_consumer(
        topic=topic,
        kafka_bootstrap=kafka_bootstrap,
        session_factory=session_factory,
        dead_letters=dead_letters,
        handler=Handler(),
    )

    await consumer.handle(record)
    [effect] = await effects(session_factory)

    assert effect.causation_id == envelope.event_id
    assert span_context_of(effect.traceparent).trace_id == upstream.get_span_context().trace_id


async def test_explicit_causation_wins_over_the_handled_event(
    session_factory: async_sessionmaker[AsyncSession],
    kafka_bootstrap: str,
    topic: str,
    dead_letters: DeadLetterPublisher,
) -> None:
    cause = uuid4()

    async def handler(session: AsyncSession, envelope: JsonEnvelope) -> None:
        await OutboxRepository(session).add(
            topic=EFFECT_TOPIC,
            aggregate_type="tests",
            aggregate_id=uuid4(),
            event_type="test.followed",
            payload=Happened(value="effect"),
            causation_id=cause,
        )

    record, _ = record_of(topic, traceparent=None)
    consumer = EventConsumer(
        topics=[topic],
        group_id=f"test-group-{uuid4().hex[:12]}",
        bootstrap_servers=kafka_bootstrap,
        session_factory=session_factory,
        dead_letters=dead_letters,
        handlers={EVENT_TYPE: handler},
    )

    await consumer.handle(record)
    [effect] = await effects(session_factory)

    assert effect.causation_id == cause


async def test_one_trace_runs_from_the_write_through_the_broker_to_the_handler(
    session_factory: async_sessionmaker[AsyncSession],
    kafka_bootstrap: str,
    topic: str,
    dead_letters: DeadLetterPublisher,
    spans: InMemorySpanExporter,
) -> None:
    request, _ = await write_in_request(session_factory, topic)
    handler = Handler()
    consumer = build_consumer(
        topic=topic,
        kafka_bootstrap=kafka_bootstrap,
        session_factory=session_factory,
        dead_letters=dead_letters,
        handler=handler,
    )

    await relay_once(session_factory, kafka_bootstrap)
    async with consumer, asyncio.timeout(30):
        while handler.calls == 0:
            await consumer.run_once()
    [effect] = await effects(session_factory)

    assert ids_of(only_span(spans, f"consume {topic}"))[0] == request.trace_id
    assert span_context_of(effect.traceparent).trace_id == request.trace_id
