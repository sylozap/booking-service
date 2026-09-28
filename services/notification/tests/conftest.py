"""Fixtures of the notification suite.

The database is created once per session and brought up by the real
migrations. Isolation is a rollback: every test gets sessions bound to one
connection with an open transaction, and whatever it writes disappears when it
ends.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from barber_common.config import Environment
from barber_common.db import Database, create_engine
from barber_common.db.session import transaction
from barber_common.testing.fixtures import (
    apply_migrations,
    create_database,
    isolated_session_factory,
)
from barber_notification.models.recipient import Recipient
from barber_notification.settings import ALEMBIC_INI, NotificationSettings

DATABASE_NAME = "notification_test"

# A DSN that points nowhere: the database of a test comes from the container.
PLACEHOLDER_DSN = "postgresql+asyncpg://notification:secret@localhost:5432/notification"

# Everything a test can write. Truncated around the tests that commit for real;
# the rest are isolated by a rollback and need no cleaning.
WRITTEN_TABLES = ("notifications", "recipients", "outbox", "processed_events")

RecipientFactory = Callable[..., Awaitable[Recipient]]


def build_settings(**overrides: object) -> NotificationSettings:
    """Settings of the service under test, with no environment behind them."""
    fields: dict[str, object] = {
        "environment": Environment.TEST,
        "database_dsn": PLACEHOLDER_DSN,
        "redis_dsn": "redis://localhost:6379/0",
        "kafka_bootstrap_servers": "localhost:9092",
    }
    fields.update(overrides)
    return NotificationSettings(**fields)  # type: ignore[arg-type]  # settings fields are typed per key


@pytest.fixture(scope="session")
def settings() -> NotificationSettings:
    return build_settings()


@pytest.fixture(scope="session")
def notification_dsn(postgres_dsn: str) -> str:
    """A database of its own for this suite, migrated to head."""
    dsn = create_database(postgres_dsn, DATABASE_NAME)
    apply_migrations(dsn=dsn, alembic_ini=ALEMBIC_INI)
    return dsn


@pytest.fixture
async def engine(notification_dsn: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(notification_dsn, pool_size=5, max_overflow=5)

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
async def concurrent_session_factory(
    notification_dsn: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions on connections of their own, cleaned up by truncating.

    Savepoints on one connection are not two transactions, so anything about
    two workers running side by side needs these.
    """
    engine = create_engine(notification_dsn, pool_size=10, max_overflow=5)
    await _truncate(engine)

    yield Database(engine).session_factory

    await _truncate(engine)
    await engine.dispose()


async def _truncate(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {', '.join(WRITTEN_TABLES)} CASCADE"))


@pytest.fixture
async def make_recipient(session: AsyncSession) -> RecipientFactory:
    """Create a recipient directly, as the events of auth would have."""

    async def factory(
        *,
        user_id: UUID | None = None,
        email: str | None = "client@example.com",
        phone: str | None = "+79990000000",
        email_confirmed: bool = True,
        telegram_chat_id: int | None = None,
        is_active: bool = True,
        preferences: dict[str, object] | None = None,
    ) -> Recipient:
        recipient = Recipient(
            user_id=user_id or uuid4(),
            email=email,
            phone=phone,
            email_confirmed=email_confirmed,
            telegram_chat_id=telegram_chat_id,
            is_active=is_active,
            preferences=preferences or {},
        )
        async with transaction(session):
            session.add(recipient)
            await session.flush()
        return recipient

    return factory
