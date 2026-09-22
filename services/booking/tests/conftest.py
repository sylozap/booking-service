"""Fixtures of the booking suite.

The database is created once per session and brought up by the real
migrations. Isolation is a rollback: every test gets sessions bound to one
connection with an open transaction, and whatever it writes disappears when it
ends.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from barber_booking.models.booking import Booking
from barber_booking.models.master_settings import MasterSettings
from barber_booking.settings import ALEMBIC_INI
from barber_common.db import create_engine
from barber_common.db.session import transaction
from barber_common.testing.fixtures import (
    apply_migrations,
    create_database,
    isolated_session_factory,
)

DATABASE_NAME = "booking_test"

MOSCOW = "Europe/Moscow"

MasterSettingsFactory = Callable[..., Awaitable[MasterSettings]]
BookingFactory = Callable[..., Awaitable[Booking]]


@pytest.fixture(scope="session")
def booking_dsn(postgres_dsn: str) -> str:
    """A database of its own for this suite, migrated to head."""
    dsn = create_database(postgres_dsn, DATABASE_NAME)
    apply_migrations(dsn=dsn, alembic_ini=ALEMBIC_INI)
    return dsn


@pytest.fixture
async def engine(booking_dsn: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(booking_dsn, pool_size=5, max_overflow=5)

    yield engine

    await engine.dispose()


@pytest.fixture
async def session_factory(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions whose writes are rolled back when the test ends."""
    async with isolated_session_factory(engine) as factory:
        yield factory


@pytest.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """One session for a test that talks to the database directly."""
    async with session_factory() as session:
        yield session


@pytest.fixture
async def make_master_settings(session: AsyncSession) -> MasterSettingsFactory:
    """Create the settings row of a master, as ``master.created`` would."""

    async def factory(
        *,
        master_id: UUID | None = None,
        salon_id: UUID | None = None,
        user_id: UUID | None = None,
        timezone: str = MOSCOW,
        buffer_after_min: int = 0,
        is_active: bool = True,
    ) -> MasterSettings:
        settings = MasterSettings(
            master_id=master_id or uuid4(),
            salon_id=salon_id or uuid4(),
            user_id=user_id or uuid4(),
            timezone=timezone,
            buffer_after_min=buffer_after_min,
            is_active=is_active,
        )
        async with transaction(session):
            session.add(settings)
            await session.flush()
        return settings

    return factory


@pytest.fixture
async def make_booking(session: AsyncSession) -> BookingFactory:
    """Create a booking directly, bypassing every rule but the database's."""

    async def factory(
        *,
        master_id: UUID,
        start_at: datetime,
        duration_min: int = 45,
        buffer_min: int = 0,
        status: str = "confirmed",
        salon_id: UUID | None = None,
    ) -> Booking:
        client_id = uuid4()
        booking = Booking(
            salon_id=salon_id or uuid4(),
            master_id=master_id,
            client_user_id=client_id,
            service_id=uuid4(),
            service_name="Haircut",
            price=Decimal("3500.00"),
            currency="RUB",
            duration_min=duration_min,
            buffer_min=buffer_min,
            start_at=start_at,
            end_at=start_at + timedelta(minutes=duration_min),
            status=status,
            created_by=client_id,
        )
        async with transaction(session):
            session.add(booking)
            await session.flush()
        return booking

    return factory
