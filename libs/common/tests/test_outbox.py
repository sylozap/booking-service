"""The outbox against a real PostgreSQL: atomicity, SKIP LOCKED, redelivery."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from barber_common.db import unit_of_work
from barber_common.events.envelope import EventEnvelope
from barber_common.kafka.producer import EventProducer
from barber_common.metrics import REGISTRY
from barber_common.outbox.models import OutboxMessage
from barber_common.outbox.relay import OutboxRelay
from barber_common.outbox.repository import OutboxRepository

pytestmark = pytest.mark.integration

TOPIC = "booking.bookings.v1"


class BookingCreated(BaseModel):
    booking_id: UUID
    master_id: UUID


@pytest.fixture
def session_factory(
    concurrent_session_factory: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    """Real connections instead of the rollback isolation of the suite.

    Every test here is about what another connection sees: whether a rolled
    back transaction left a row, whether a second relay can claim what the
    first one holds. Sessions sharing one connection would answer all of that
    with "yes" and prove nothing.
    """
    return concurrent_session_factory


@dataclass(frozen=True, slots=True)
class PublishedEvent:
    """What the relay handed to the broker, kept for the assertions."""

    topic: str
    key: str
    event_id: UUID
    event_type: str


class PublishFailed(RuntimeError):
    """Stands in for a broker that accepted the message and then went away."""


class RecordingProducer(EventProducer):
    """A producer that keeps what it was asked to send instead of sending it.

    Kafka has its own tests; what matters here is what the relay does with the
    rows around the call, so the broker is replaced and the database is real.
    """

    def __init__(self, *, fail_times: int = 0) -> None:
        self.producer_name = "booking@test"
        # There is no broker behind this one, so it is born started: the relay
        # connects before publishing and would otherwise reach into aiokafka.
        self._is_started = True
        self.published: list[PublishedEvent] = []
        self._fail_times = fail_times

    async def publish[PayloadT](
        self,
        *,
        topic: str,
        aggregate_id: UUID | str,
        envelope: EventEnvelope[PayloadT],
        traceparent: str | None = None,
    ) -> None:
        self.published.append(
            PublishedEvent(
                topic=topic,
                key=str(aggregate_id),
                event_id=envelope.event_id,
                event_type=envelope.event_type,
            )
        )
        if self._fail_times > 0:
            self._fail_times -= 1
            raise PublishFailed


def booking_event() -> BookingCreated:
    return BookingCreated(booking_id=uuid4(), master_id=uuid4())


async def add_event(
    session_factory: async_sessionmaker[AsyncSession], payload: BookingCreated
) -> UUID:
    async with unit_of_work(session_factory) as session:
        message = await OutboxRepository(session).add(
            topic=TOPIC,
            aggregate_type="bookings",
            aggregate_id=payload.booking_id,
            event_type="booking.created",
            payload=payload,
        )
        return message.id


async def count_rows(engine: AsyncEngine, *, published: bool | None = None) -> int:
    statement = select(func.count()).select_from(OutboxMessage)
    if published is True:
        statement = statement.where(OutboxMessage.published_at.is_not(None))
    elif published is False:
        statement = statement.where(OutboxMessage.published_at.is_(None))

    async with engine.connect() as connection:
        result = await connection.execute(statement)
        return int(result.scalar_one())


async def test_a_rolled_back_transaction_leaves_no_event(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    with pytest.raises(PublishFailed):
        async with unit_of_work(session_factory) as session:
            await OutboxRepository(session).add(
                topic=TOPIC,
                aggregate_type="bookings",
                aggregate_id=uuid4(),
                event_type="booking.created",
                payload=booking_event(),
            )
            raise PublishFailed

    assert await count_rows(engine) == 0


async def test_relay_publishes_a_pending_event_and_marks_it(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    payload = booking_event()
    event_id = await add_event(session_factory, payload)
    producer = RecordingProducer()
    relay = OutboxRelay(session_factory=session_factory, producer=producer)

    published = await relay.run_once()

    sent = producer.published[0]
    assert published == 1
    assert sent.topic == TOPIC
    assert sent.key == str(payload.booking_id)
    assert sent.event_id == event_id
    assert sent.event_type == "booking.created"
    assert await count_rows(engine, published=False) == 0


async def test_relay_reports_nothing_to_do_on_an_empty_outbox(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    relay = OutboxRelay(session_factory=session_factory, producer=RecordingProducer())

    assert await relay.run_once() == 0


async def test_two_relays_do_not_publish_the_same_event_twice(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    for _ in range(5):
        await add_event(session_factory, booking_event())

    async with unit_of_work(session_factory) as first_session:
        claimed_by_first = await OutboxRepository(first_session).claim_batch(limit=100)

        async with unit_of_work(session_factory) as second_session:
            claimed_by_second = await OutboxRepository(second_session).claim_batch(limit=100)

        first_ids = {message.id for message in claimed_by_first}
        second_ids = {message.id for message in claimed_by_second}

    assert len(first_ids) == 5
    assert second_ids == set()


async def test_a_failure_before_the_mark_republishes_instead_of_losing(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    event_id = await add_event(session_factory, booking_event())
    producer = RecordingProducer(fail_times=1)
    relay = OutboxRelay(session_factory=session_factory, producer=producer)

    with pytest.raises(PublishFailed):
        await relay.run_once()

    assert await count_rows(engine, published=False) == 1

    published = await relay.run_once()

    assert published == 1
    assert [event.event_id for event in producer.published] == [
        event_id,
        event_id,
    ]
    assert await count_rows(engine, published=False) == 0


async def test_pending_events_survive_a_broker_outage(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    for _ in range(3):
        await add_event(session_factory, booking_event())
    relay = OutboxRelay(session_factory=session_factory, producer=RecordingProducer(fail_times=10))

    for _ in range(3):
        await relay._pass()  # the supervised pass swallows the outage

    assert await count_rows(engine, published=False) == 3

    working = OutboxRelay(session_factory=session_factory, producer=RecordingProducer())

    assert await working.run_once() == 3
    assert await count_rows(engine, published=False) == 0


class HangingProducer(RecordingProducer):
    """A broker that went away mid-send: the client waits with no deadline."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def publish[PayloadT](
        self,
        *,
        topic: str,
        aggregate_id: UUID | str,
        envelope: EventEnvelope[PayloadT],
        traceparent: str | None = None,
    ) -> None:
        await self.release.wait()
        raise PublishFailed


async def gauge_reaches(name: str, value: float, *, timeout_seconds: float = 10.0) -> None:
    # Polled: the gauge is set by a loop of the relay, which announces nothing.
    async with asyncio.timeout(timeout_seconds):
        while True:
            if REGISTRY.get_sample_value(name) == value:
                return
            await asyncio.sleep(0.02)


async def test_the_gauges_follow_the_outbox_while_a_pass_hangs(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The last good measurement saw an empty outbox: what a stale gauge keeps.
    await OutboxRelay(session_factory=session_factory, producer=RecordingProducer()).measure_once()
    assert REGISTRY.get_sample_value("outbox_pending_messages") == 0
    for _ in range(3):
        await add_event(session_factory, booking_event())
    producer = HangingProducer()
    relay = OutboxRelay(
        session_factory=session_factory,
        producer=producer,
        measure_interval_seconds=0.05,
        publish_timeout_seconds=60,
    )

    async with relay.run_in_background():
        # The pass holds the three rows locked and never returns.
        await gauge_reaches("outbox_pending_messages", 3)
        lag = REGISTRY.get_sample_value("outbox_publish_lag_seconds")
        producer.release.set()

    assert lag is not None
    assert lag > 0


async def test_a_send_that_never_returns_is_given_up_after_the_deadline(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await add_event(session_factory, booking_event())
    relay = OutboxRelay(
        session_factory=session_factory,
        producer=HangingProducer(),
        publish_timeout_seconds=0.1,
    )

    with pytest.raises(TimeoutError):
        await relay.run_once()

    # Rolled back: the row is there for the next pass, and no longer locked.
    assert await count_rows(engine, published=False) == 1
    working = OutboxRelay(session_factory=session_factory, producer=RecordingProducer())
    assert await working.run_once() == 1


async def test_a_relay_stuck_in_a_send_still_stops(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await add_event(session_factory, booking_event())
    relay = OutboxRelay(
        session_factory=session_factory,
        producer=HangingProducer(),
        idle_interval_seconds=0.01,
        publish_timeout_seconds=0.1,
    )

    # The shutdown of the service waits for this block to end.
    async with asyncio.timeout(5):
        async with relay.run_in_background():
            await asyncio.sleep(0.05)


async def test_a_batch_queues_one_event_per_aggregate_with_one_cause(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    payloads = [booking_event() for _ in range(5)]
    cause = uuid4()

    async with unit_of_work(session_factory) as session:
        messages = await OutboxRepository(session).add_batch(
            topic=TOPIC,
            aggregate_type="bookings",
            event_type="booking.cancelled",
            events=[(payload.booking_id, payload) for payload in payloads],
            causation_id=cause,
        )

    assert [message.aggregate_id for message in messages] == [p.booking_id for p in payloads]
    assert {message.causation_id for message in messages} == {cause}
    assert len({message.id for message in messages}) == 5
    assert await count_rows(engine, published=False) == 5
