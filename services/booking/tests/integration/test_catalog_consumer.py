"""Events of catalog retire what booking cached about it.

The handlers run through the real consumer runner, with the deduplication
table of the real database and a real Redis. The last test goes through a real
broker as well.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from aiokafka import AIOKafkaConsumer, ConsumerRecord
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from barber_booking.consumers.catalog_events import CATALOG_TOPICS, CatalogCacheInvalidation
from barber_booking.domain.identifiers import MasterId, ServiceId
from barber_booking.services.cache import BookingCache
from barber_common.cache import Cache
from barber_common.events.catalog import (
    CATALOG_MASTERS_TOPIC,
    CATALOG_SERVICES_TOPIC,
    MasterDeactivated,
    MasterEventType,
    MasterUpdated,
    ServiceEventType,
    ServiceUpdated,
)
from barber_common.events.envelope import build_envelope
from barber_common.kafka import EventConsumer, EventProducer, ProcessingResult

pytestmark = pytest.mark.integration

CACHED = b'{"cached": true}'


def record(topic: str, event_type: str, payload: dict[str, object]) -> ConsumerRecord[bytes, bytes]:
    envelope = build_envelope(event_type=event_type, payload=payload, producer="catalog@test")
    value = envelope.model_dump_json().encode("utf-8")
    return ConsumerRecord(
        topic=topic,
        partition=0,
        offset=0,
        timestamp=0,
        timestamp_type=0,
        key=None,
        value=value,
        checksum=None,
        serialized_key_size=0,
        serialized_value_size=len(value),
        headers=[],
    )


def master_updated(master_id: UUID) -> ConsumerRecord[bytes, bytes]:
    payload = MasterUpdated(
        master_id=master_id,
        salon_id=uuid4(),
        user_id=uuid4(),
        display_name="Ivan",
        specialization="Barber",
        is_active=True,
    )
    return record(CATALOG_MASTERS_TOPIC, MasterEventType.UPDATED, payload.model_dump(mode="json"))


def service_updated(service_id: UUID) -> ServiceUpdated:
    return ServiceUpdated(
        service_id=service_id,
        salon_id=uuid4(),
        name="Haircut",
        base_duration_min=60,
        base_price=Decimal("3500.00"),
        currency="RUB",
    )


@pytest.fixture
def booking_cache(cache: Cache) -> BookingCache:
    return BookingCache(cache)


@pytest.fixture
def offerings(cache: Cache) -> CachedOfferings:
    return CachedOfferings(cache)


@pytest.fixture
def consumer(
    session_factory: async_sessionmaker[AsyncSession], booking_cache: BookingCache
) -> EventConsumer:
    """The runner over a client that is never started: ``handle`` needs none."""
    return EventConsumer(
        topics=CATALOG_TOPICS,
        group_id="booking.catalog",
        session_factory=session_factory,
        dead_letters=MagicMock(),
        handlers=CatalogCacheInvalidation(booking_cache).handlers(),
        client=MagicMock(),
    )


class CachedOfferings:
    """Writes under the key scheme of booking, and asks what is still readable."""

    def __init__(self, cache: Cache) -> None:
        self._raw = cache
        self._scheme = BookingCache(cache)

    async def store(self, master_id: UUID, service_id: UUID) -> None:
        key = await self._scheme.offering_key(MasterId(master_id), ServiceId(service_id))
        await self._raw.set(key, CACHED)

    async def is_readable(self, master_id: UUID, service_id: UUID) -> bool:
        key = await self._scheme.offering_key(MasterId(master_id), ServiceId(service_id))
        return await self._raw.get(key) == CACHED


async def test_master_updated_retires_what_was_cached_about_the_master(
    consumer: EventConsumer, offerings: CachedOfferings
) -> None:
    master_id, service_id = uuid4(), uuid4()
    await offerings.store(master_id, service_id)

    result = await consumer.handle(master_updated(master_id))

    assert result is ProcessingResult.OK
    assert not await offerings.is_readable(master_id, service_id)


async def test_another_master_keeps_their_cached_answers(
    consumer: EventConsumer, offerings: CachedOfferings
) -> None:
    changed, untouched, service_id = uuid4(), uuid4(), uuid4()
    await offerings.store(untouched, service_id)

    await consumer.handle(master_updated(changed))

    assert await offerings.is_readable(untouched, service_id)


async def test_service_updated_retires_the_service_for_every_master(
    consumer: EventConsumer, offerings: CachedOfferings
) -> None:
    first, second, service_id = uuid4(), uuid4(), uuid4()
    await offerings.store(first, service_id)
    await offerings.store(second, service_id)
    event = service_updated(service_id)

    await consumer.handle(
        record(CATALOG_SERVICES_TOPIC, ServiceEventType.UPDATED, event.model_dump(mode="json"))
    )

    assert not await offerings.is_readable(first, service_id)
    assert not await offerings.is_readable(second, service_id)


async def test_master_deactivated_retires_the_cached_active_flag(
    consumer: EventConsumer, offerings: CachedOfferings
) -> None:
    master_id, service_id = uuid4(), uuid4()
    await offerings.store(master_id, service_id)
    event = MasterDeactivated(master_id=master_id, salon_id=uuid4())

    await consumer.handle(
        record(CATALOG_MASTERS_TOPIC, MasterEventType.DEACTIVATED, event.model_dump(mode="json"))
    )

    assert not await offerings.is_readable(master_id, service_id)


async def test_a_duplicate_delivery_has_no_second_effect(
    consumer: EventConsumer, cache: Cache
) -> None:
    master_id = uuid4()
    message = master_updated(master_id)

    first = await consumer.handle(message)
    second = await consumer.handle(message)

    assert (first, second) == (ProcessingResult.OK, ProcessingResult.DUPLICATE)
    assert await cache.get(f"booking:generation:master:{master_id}") == b"1"


async def test_an_unknown_event_type_is_skipped(
    consumer: EventConsumer, offerings: CachedOfferings
) -> None:
    master_id, service_id = uuid4(), uuid4()
    await offerings.store(master_id, service_id)

    result = await consumer.handle(
        record(CATALOG_MASTERS_TOPIC, "master.promoted", {"master_id": str(master_id)})
    )

    assert result is ProcessingResult.SKIPPED
    assert await offerings.is_readable(master_id, service_id)


# --- through the broker -----------------------------------------------------


@pytest.fixture
async def producer(kafka_bootstrap: str) -> AsyncIterator[EventProducer]:
    async with EventProducer(bootstrap_servers=kafka_bootstrap, service_name="catalog") as producer:
        yield producer


async def test_a_changed_duration_reaches_the_cache_within_a_second(
    kafka_bootstrap: str,
    session_factory: async_sessionmaker[AsyncSession],
    booking_cache: BookingCache,
    offerings: CachedOfferings,
    producer: EventProducer,
) -> None:
    master_id, service_id = uuid4(), uuid4()
    await offerings.store(master_id, service_id)
    client: AIOKafkaConsumer[bytes, bytes] = AIOKafkaConsumer(
        *CATALOG_TOPICS,
        bootstrap_servers=kafka_bootstrap,
        group_id=f"booking.catalog.test-{uuid4().hex[:8]}",
        enable_auto_commit=False,
        auto_offset_reset="earliest",
    )
    consumer = EventConsumer(
        topics=CATALOG_TOPICS,
        group_id="booking.catalog",
        session_factory=session_factory,
        dead_letters=MagicMock(),
        handlers=CatalogCacheInvalidation(booking_cache).handlers(),
        poll_timeout_ms=100,
        client=client,
    )
    # The topics have to exist before the group can be assigned them.
    event = service_updated(service_id)
    await producer.publish(
        topic=CATALOG_SERVICES_TOPIC,
        aggregate_id=uuid4(),
        envelope=build_envelope(event_type="service.warmup", payload=event, producer="catalog"),
    )
    await producer.publish(
        topic=CATALOG_MASTERS_TOPIC,
        aggregate_id=uuid4(),
        envelope=build_envelope(event_type="master.warmup", payload=event, producer="catalog"),
    )

    async with consumer.run_in_background():
        async with asyncio.timeout(30.0):
            # Polling: the assignment happens inside the client, with no event.
            while {tp.topic for tp in client.assignment()} != set(CATALOG_TOPICS):  # noqa: ASYNC110
                await asyncio.sleep(0.05)

        published_at = time.monotonic()
        await producer.publish(
            topic=CATALOG_SERVICES_TOPIC,
            aggregate_id=service_id,
            envelope=build_envelope(
                event_type=ServiceEventType.UPDATED, payload=event, producer="catalog"
            ),
        )
        async with asyncio.timeout(1.0):
            while await offerings.is_readable(master_id, service_id):  # noqa: ASYNC110
                await asyncio.sleep(0.02)
        elapsed = time.monotonic() - published_at
    await consumer.stop()

    assert elapsed < 1.0
