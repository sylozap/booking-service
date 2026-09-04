"""The producer against a real broker: key, headers and a readable envelope."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from uuid import uuid4

import pytest
from aiokafka import AIOKafkaConsumer
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util._once import Once
from pydantic import BaseModel

from barber_common.events.envelope import JsonEnvelope, build_envelope
from barber_common.kafka.producer import EventProducer
from barber_common.tracing import TRACEPARENT_HEADER

pytestmark = pytest.mark.integration


class BookingCreated(BaseModel):
    booking_id: str
    master_id: str
    service_name: str


@pytest.fixture
def topic() -> str:
    """A topic of its own per test.

    The broker lives for the whole session, so a shared topic would let one
    test read the message of another and pass for the wrong reason.
    """
    return f"booking.bookings.v1.{uuid4().hex[:12]}"


@pytest.fixture
async def producer(kafka_bootstrap: str) -> AsyncIterator[EventProducer]:
    async with EventProducer(
        bootstrap_servers=kafka_bootstrap,
        service_name="booking",
        service_version="1.4.0",
    ) as producer:
        yield producer


@pytest.fixture
def tracing() -> Iterator[None]:
    """Record spans in memory, so the producer has a trace context to inject."""
    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE = Once()

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(InMemorySpanExporter()))
    trace.set_tracer_provider(provider)

    yield

    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE = Once()


def booking_created() -> BookingCreated:
    return BookingCreated(booking_id=str(uuid4()), master_id=str(uuid4()), service_name="Haircut")


async def read_one(bootstrap: str, topic: str) -> tuple[bytes, bytes, dict[str, bytes]]:
    """Read the first message of a topic, from the beginning."""
    consumer = AIOKafkaConsumer(
        topic,
        bootstrap_servers=bootstrap,
        auto_offset_reset="earliest",
        group_id=f"test-{uuid4().hex}",
        enable_auto_commit=False,
    )
    await consumer.start()
    try:
        message = await consumer.getone()
    finally:
        await consumer.stop()
    return message.key, message.value, dict(message.headers)


async def test_event_reaches_the_topic_with_the_aggregate_id_as_the_key(
    producer: EventProducer, kafka_bootstrap: str, topic: str
) -> None:
    booking_id = uuid4()
    envelope = build_envelope(
        event_type="booking.created",
        payload=booking_created(),
        producer=producer.producer_name,
    )

    await producer.publish(topic=topic, aggregate_id=booking_id, envelope=envelope)
    key, _, _ = await read_one(kafka_bootstrap, topic)

    assert key.decode() == str(booking_id)


async def test_headers_carry_the_event_type_and_the_correlation_id(
    producer: EventProducer, kafka_bootstrap: str, topic: str
) -> None:
    envelope = build_envelope(
        event_type="booking.created",
        payload=booking_created(),
        producer=producer.producer_name,
        correlation_id="c-8f2a",
    )

    await producer.publish(topic=topic, aggregate_id=uuid4(), envelope=envelope)
    _, _, headers = await read_one(kafka_bootstrap, topic)

    assert headers["event_type"].decode() == "booking.created"
    assert headers["correlation_id"].decode() == "c-8f2a"
    assert headers["event_id"].decode() == str(envelope.event_id)


async def test_traceparent_travels_with_the_message(
    producer: EventProducer, kafka_bootstrap: str, topic: str, tracing: None
) -> None:
    envelope = build_envelope(
        event_type="booking.created",
        payload=booking_created(),
        producer=producer.producer_name,
    )

    await producer.publish(topic=topic, aggregate_id=uuid4(), envelope=envelope)
    _, _, headers = await read_one(kafka_bootstrap, topic)

    assert TRACEPARENT_HEADER in headers
    assert headers[TRACEPARENT_HEADER].decode().startswith("00-")


async def test_envelope_parses_back_into_the_model(
    producer: EventProducer, kafka_bootstrap: str, topic: str
) -> None:
    payload = booking_created()
    envelope = build_envelope(
        event_type="booking.created",
        payload=payload,
        producer=producer.producer_name,
    )

    await producer.publish(topic=topic, aggregate_id=uuid4(), envelope=envelope)
    _, value, _ = await read_one(kafka_bootstrap, topic)
    received = JsonEnvelope.model_validate(json.loads(value))

    assert received.event_id == envelope.event_id
    assert received.event_type == "booking.created"
    assert received.producer == "booking@1.4.0"
    assert received.payload["booking_id"] == payload.booking_id
