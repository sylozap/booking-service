"""The reminder scheduler against a real database.

The outbox stands for Kafka: a reminder is published once the transaction that
wrote its row commits, and the relay is tested on its own.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from barber_booking.domain.identifiers import BookingId
from barber_booking.models.booking import Booking
from barber_booking.models.master_settings import MasterSettings
from barber_booking.repositories.bookings import BookingRepository
from barber_booking.workers.reminders import ReminderScheduler
from barber_common.db.session import transaction
from barber_common.events.bookings import REMINDERS_TOPIC, ReminderEventType
from barber_common.metrics import REGISTRY
from barber_common.outbox.models import OutboxMessage

pytestmark = pytest.mark.integration

MasterSettingsFactory = Callable[..., Awaitable[MasterSettings]]

NOW = datetime(2030, 3, 4, 7, 0, tzinfo=UTC)
LEAD = timedelta(hours=4)


class Clock:
    """A clock the test moves by hand."""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def scheduler(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock | None = None,
    *,
    batch_size: int = 100,
) -> ReminderScheduler:
    return ReminderScheduler(
        session_factory=session_factory, clock=clock or Clock(), batch_size=batch_size
    )


async def add_booking(
    session: AsyncSession,
    *,
    start_at: datetime,
    reminder_at: datetime | None = None,
    status: str = "confirmed",
    master_id: UUID | None = None,
) -> Booking:
    """A booking as the scenarios leave it, its reminder ``LEAD`` ahead by default."""
    client_id = uuid4()
    booking = Booking(
        salon_id=uuid4(),
        master_id=master_id or uuid4(),
        client_user_id=client_id,
        service_id=uuid4(),
        service_name="Haircut",
        price=Decimal("3500.00"),
        currency="RUB",
        duration_min=45,
        start_at=start_at,
        end_at=start_at + timedelta(minutes=45),
        status=status,
        created_by=client_id,
        reminder_at=reminder_at or start_at - LEAD,
    )
    async with transaction(session):
        session.add(booking)
        await session.flush()
    return booking


async def reminders(session: AsyncSession) -> list[OutboxMessage]:
    statement = (
        select(OutboxMessage)
        .where(OutboxMessage.topic == REMINDERS_TOPIC)
        .order_by(OutboxMessage.created_at)
    )
    return list((await session.execute(statement)).scalars().all())


async def reminder_sent_at(session: AsyncSession, booking_id: UUID) -> datetime | None:
    statement = (
        select(Booking.reminder_sent_at)
        .where(Booking.id == booking_id)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(statement)).scalar_one()


async def test_a_due_reminder_is_published_once(
    session_factory: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    make_master_settings: MasterSettingsFactory,
) -> None:
    master = await make_master_settings(timezone="Asia/Yekaterinburg")
    booking = await add_booking(
        session, start_at=NOW + LEAD - timedelta(minutes=1), master_id=master.master_id
    )
    worker = scheduler(session_factory)

    await worker.run_once()
    await worker.run_once()

    [published] = await reminders(session)
    assert published.event_type == ReminderEventType.DUE
    assert (published.aggregate_type, published.aggregate_id) == ("bookings", booking.id)
    assert published.payload == {
        "booking_id": str(booking.id),
        "salon_id": str(booking.salon_id),
        "master_id": str(master.master_id),
        "client_user_id": str(booking.client_user_id),
        "service_name": "Haircut",
        "start_at": booking.start_at.isoformat().replace("+00:00", "Z"),
        "end_at": booking.end_at.isoformat().replace("+00:00", "Z"),
        "hours_before": 4,
        "timezone": "Asia/Yekaterinburg",
    }
    assert await reminder_sent_at(session, booking.id) == NOW


async def test_a_reminder_of_a_master_without_settings_has_no_zone(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    await add_booking(session, start_at=NOW + timedelta(hours=1))

    await scheduler(session_factory).run_once()

    [published] = await reminders(session)
    assert published.payload["timezone"] is None


async def test_a_reminder_not_yet_due_waits(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    booking = await add_booking(session, start_at=NOW + LEAD + timedelta(minutes=1))

    await scheduler(session_factory).run_once()

    assert await reminders(session) == []
    assert await reminder_sent_at(session, booking.id) is None


@pytest.mark.parametrize("status", ["cancelled_by_client", "cancelled_by_salon"])
async def test_a_cancelled_booking_gets_no_reminder(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession, status: str
) -> None:
    await add_booking(session, start_at=NOW + timedelta(hours=1), status=status)

    await scheduler(session_factory).run_once()

    assert await reminders(session) == []


async def test_a_moved_booking_is_reminded_of_its_new_time(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    booking = await add_booking(session, start_at=NOW + timedelta(hours=1))
    clock = Clock()
    worker = scheduler(session_factory, clock)
    await worker.run_once()
    new_start = NOW + timedelta(days=1)
    async with transaction(session):
        repository = BookingRepository(session)
        current = await repository.lock(BookingId(booking.id))
        assert current is not None
        # What a reschedule leaves behind: a new time, its reminder due anew.
        await repository.save(
            replace(
                current, start_at=new_start, reminder_at=new_start - LEAD, reminder_sent_at=None
            )
        )

    await worker.run_once()
    clock.now = new_start - LEAD
    await worker.run_once()

    first, second = await reminders(session)
    assert first.payload["start_at"] == "2030-03-04T08:00:00Z"
    assert second.payload["start_at"] == "2030-03-05T07:00:00Z"


async def test_a_visit_that_has_begun_is_not_reminded_of(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    # The scheduler was down past the start of the visit.
    booking = await add_booking(session, start_at=NOW - timedelta(minutes=10))

    taken = await scheduler(session_factory).run_once()

    assert taken == 1
    assert await reminders(session) == []
    # Stamped all the same, so it stops being a candidate on every pass.
    assert await reminder_sent_at(session, booking.id) == NOW


async def test_a_full_batch_leaves_the_rest_to_the_next_pass(
    session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    for minutes in range(3):
        await add_booking(session, start_at=NOW + timedelta(hours=1, minutes=minutes))
    worker = scheduler(session_factory, batch_size=2)

    assert await worker.run_once() == 2
    assert await worker.run_once() == 1
    assert len(await reminders(session)) == 3


async def test_the_last_pass_is_recorded_for_the_alert(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await scheduler(session_factory).run_once()

    assert REGISTRY.get_sample_value("reminder_scheduler_last_run_timestamp") == NOW.timestamp()


async def test_two_replicas_never_publish_a_reminder_twice(
    concurrent_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with concurrent_session_factory() as session:
        booked = [
            await add_booking(session, start_at=NOW + timedelta(hours=1, minutes=minutes))
            for minutes in range(10)
        ]
    # Small batches, so both replicas really compete for the same rows.
    first = scheduler(concurrent_session_factory, batch_size=3)
    second = scheduler(concurrent_session_factory, batch_size=3)

    for _ in range(4):
        await asyncio.gather(first.run_once(), second.run_once())

    async with concurrent_session_factory() as session:
        published = await reminders(session)
    assert sorted(message.aggregate_id for message in published) == sorted(
        booking.id for booking in booked
    )


class PodKilled(Exception):
    """The commit that never happened."""


async def test_a_pass_that_dies_before_the_commit_loses_nothing_and_repeats_nothing(
    concurrent_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with concurrent_session_factory() as session:
        booking = await add_booking(session, start_at=NOW + timedelta(hours=1))
    engine = concurrent_session_factory.kw["bind"]
    assert isinstance(engine, AsyncEngine)
    worker = scheduler(concurrent_session_factory)

    def kill_the_pod(_connection: Connection) -> None:
        raise PodKilled

    # Between the outbox row and the stamp being written, and the commit.
    event.listen(engine.sync_engine, "commit", kill_the_pod)
    try:
        with pytest.raises(PodKilled):
            await worker.run_once()
    finally:
        event.remove(engine.sync_engine, "commit", kill_the_pod)

    async with concurrent_session_factory() as session:
        assert await reminders(session) == []
        assert await reminder_sent_at(session, booking.id) is None

    await worker.run_once()
    await worker.run_once()

    async with concurrent_session_factory() as session:
        assert [message.aggregate_id for message in await reminders(session)] == [booking.id]
