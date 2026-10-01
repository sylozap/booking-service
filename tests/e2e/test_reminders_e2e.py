"""A reminder from the booking that asks for it to the message that delivers it.

The whole path is real except its two ends: the booking is written by the
repository of booking the way the creation scenario writes it, and the message
lands in a provider that records it instead of in Telegram. Between them run
the reminder scheduler, the outbox relay of booking, a Kafka broker, the
consumer group of notification and its delivery worker, each over the database
of its own service.

The only test that imports two services: it checks the contract between them,
which neither suite can do alone.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from barber_booking.domain.booking import Actor, Booking, ServiceSnapshot, reminder_for
from barber_booking.domain.identifiers import BookingId, MasterId, SalonId, ServiceId, UserId
from barber_booking.models.master_settings import MasterSettings
from barber_booking.repositories.bookings import BookingRepository
from barber_booking.settings import ALEMBIC_INI as BOOKING_ALEMBIC_INI
from barber_booking.workers.reminders import ReminderScheduler
from barber_common.db import Database, create_engine
from barber_common.db.session import transaction
from barber_common.kafka import DeadLetterPublisher, EventConsumer, EventProducer
from barber_common.outbox import OutboxRelay
from barber_common.testing.fixtures import apply_migrations, create_database
from barber_notification.consumers.booking_events import BOOKING_EVENTS_TOPICS, BookingEvents
from barber_notification.models.recipient import Recipient
from barber_notification.providers.base import Channel, DeliveryResult, Message
from barber_notification.providers.registry import ProviderRegistry
from barber_notification.settings import ALEMBIC_INI as NOTIFICATION_ALEMBIC_INI
from barber_notification.workers.delivery import DeliveryWorker

pytestmark = pytest.mark.integration

LEAD = timedelta(hours=4)
# The whole path, broker included, has this long to deliver.
DEADLINE_SECONDS = 60.0
MOSCOW = "Europe/Moscow"


class RecordingProvider:
    """Telegram, as far as this test is concerned: it remembers what it got."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Message]] = []

    @property
    def name(self) -> str:
        return "recording"

    async def send(self, address: str, message: Message) -> DeliveryResult:
        self.sent.append((address, message))
        return DeliveryResult.sent()


@pytest.fixture(scope="module")
def booking_dsn(postgres_dsn: str) -> str:
    dsn = create_database(postgres_dsn, "booking_e2e")
    apply_migrations(dsn=dsn, alembic_ini=BOOKING_ALEMBIC_INI)
    return dsn


@pytest.fixture(scope="module")
def notification_dsn(postgres_dsn: str) -> str:
    dsn = create_database(postgres_dsn, "notification_e2e")
    apply_migrations(dsn=dsn, alembic_ini=NOTIFICATION_ALEMBIC_INI)
    return dsn


async def _database(dsn: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine: AsyncEngine = create_engine(dsn, pool_size=5, max_overflow=0)
    yield Database(engine).session_factory
    await engine.dispose()


@pytest.fixture
async def booking_db(booking_dsn: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async for factory in _database(booking_dsn):
        yield factory


@pytest.fixture
async def notification_db(
    notification_dsn: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async for factory in _database(notification_dsn):
        yield factory


@dataclass
class Platform:
    """The two services, joined by the broker, one pass at a time."""

    booking_db: async_sessionmaker[AsyncSession]
    notification_db: async_sessionmaker[AsyncSession]
    scheduler: ReminderScheduler
    relay: OutboxRelay
    consumer: EventConsumer
    delivery: DeliveryWorker
    telegram: RecordingProvider

    async def run_until(self, condition: Callable[[], bool]) -> None:
        """Turn every wheel once per round until the condition holds.

        Rounds rather than a sleep: the consumer waits on the broker for up to a
        second per round, and the first rounds go on joining the group.
        """
        async with asyncio.timeout(DEADLINE_SECONDS):
            while not condition():
                await self.scheduler.run_once()
                await self.relay.run_once()
                await self.consumer.run_once()
                await self.delivery.run_once()


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
async def platform(
    kafka_bootstrap: str,
    booking_db: async_sessionmaker[AsyncSession],
    notification_db: async_sessionmaker[AsyncSession],
) -> AsyncIterator[Platform]:
    producer = EventProducer(bootstrap_servers=kafka_bootstrap, service_name="booking")
    dead_letters = DeadLetterPublisher(bootstrap_servers=kafka_bootstrap)
    consumer = EventConsumer(
        topics=BOOKING_EVENTS_TOPICS,
        # A group of its own per test: it reads the topic from the beginning.
        group_id=f"notification.bookings.e2e-{uuid4()}",
        bootstrap_servers=kafka_bootstrap,
        session_factory=notification_db,
        dead_letters=dead_letters,
        handlers=BookingEvents().handlers(),
    )
    telegram = RecordingProvider()
    # The scheduler runs two minutes ahead: the reminder of a booking made now
    # for four hours and a minute from now is due by then.
    clock = Clock(datetime.now(UTC) + timedelta(minutes=2))

    yield Platform(
        booking_db=booking_db,
        notification_db=notification_db,
        scheduler=ReminderScheduler(session_factory=booking_db, clock=clock),
        relay=OutboxRelay(session_factory=booking_db, producer=producer),
        consumer=consumer,
        delivery=DeliveryWorker(
            session_factory=notification_db,
            providers=ProviderRegistry({Channel.EMAIL: telegram, Channel.TELEGRAM: telegram}),
        ),
        telegram=telegram,
    )

    await consumer.stop()
    await dead_letters.stop()
    await producer.stop()


async def a_client_on_telegram(platform: Platform, chat_id: int) -> UUID:
    """A recipient as the events of auth and a linked chat leave it."""
    user_id = uuid4()
    async with platform.notification_db() as session, transaction(session):
        session.add(Recipient(user_id=user_id, email=None, telegram_chat_id=chat_id))
    return user_id


async def a_booking(platform: Platform, client_user_id: UUID, *, start_in: timedelta) -> BookingId:
    """A booking as the creation scenario writes it, with its reminder computed."""
    now = datetime.now(UTC)
    master_id = uuid4()
    start_at = now + start_in
    booking = Booking.confirmed(
        id=BookingId(uuid4()),
        salon_id=SalonId(uuid4()),
        master_id=MasterId(master_id),
        client_user_id=UserId(client_user_id),
        service=ServiceSnapshot(
            service_id=ServiceId(uuid4()),
            name="Стрижка",
            price=Decimal("3500.00"),
            currency="RUB",
            duration_min=45,
        ),
        buffer_min=0,
        cancel_deadline_min=240,
        start_at=start_at,
        reminder_at=reminder_for(start_at, now=now, lead=LEAD),
        by=Actor(user_id=UserId(client_user_id), is_client=True),
    )
    async with platform.booking_db() as session, transaction(session):
        session.add(
            MasterSettings(master_id=master_id, salon_id=uuid4(), user_id=uuid4(), timezone=MOSCOW)
        )
        await BookingRepository(session).add(booking)
    return booking.id


async def cancel(platform: Platform, booking_id: BookingId) -> None:
    async with platform.booking_db() as session, transaction(session):
        repository = BookingRepository(session)
        booking = await repository.lock(booking_id)
        assert booking is not None
        await repository.save(booking.cancel(by=Actor.the_salon(), now=datetime.now(UTC)))


def messages_to(platform: Platform, chat_id: int) -> list[Message]:
    return [message for address, message in platform.telegram.sent if address == str(chat_id)]


async def test_a_booking_made_shortly_ahead_ends_as_a_reminder_in_the_chat(
    platform: Platform,
) -> None:
    client = await a_client_on_telegram(platform, chat_id=101)
    await a_booking(platform, client, start_in=LEAD + timedelta(minutes=1))

    await platform.run_until(lambda: bool(messages_to(platform, 101)))

    [message] = messages_to(platform, 101)
    assert message.template == "booking_reminder"
    assert "Стрижка" in message.body
    assert "(Europe/Moscow)" in message.body


async def test_a_cancelled_booking_gets_no_reminder(platform: Platform) -> None:
    cancelled_client = await a_client_on_telegram(platform, chat_id=201)
    cancelled = await a_booking(platform, cancelled_client, start_in=LEAD + timedelta(minutes=1))
    await cancel(platform, cancelled)
    # A booking alongside it that is not cancelled: once its reminder is in,
    # the one of the cancelled booking would have been too.
    control_client = await a_client_on_telegram(platform, chat_id=202)
    await a_booking(platform, control_client, start_in=LEAD + timedelta(minutes=1))

    await platform.run_until(lambda: bool(messages_to(platform, 202)))

    assert messages_to(platform, 201) == []
