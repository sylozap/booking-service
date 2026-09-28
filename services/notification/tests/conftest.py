"""Fixtures of the notification suite.

The database is created once per session and brought up by the real
migrations. Isolation is a rollback: every test gets sessions bound to one
connection with an open transaction, and whatever it writes disappears when it
ends.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from barber_common.auth import ACCESS_TOKEN_TYPE, StaticKeys, TokenVerifier, use_authentication
from barber_common.config import Environment
from barber_common.db import Database, create_engine, get_session
from barber_common.db.session import transaction
from barber_common.testing.fixtures import (
    apply_migrations,
    create_database,
    isolated_session_factory,
)
from barber_notification.main import create_application
from barber_notification.models.recipient import Recipient
from barber_notification.settings import ALEMBIC_INI, NotificationSettings

DATABASE_NAME = "notification_test"

ISSUER = "https://barber.local/auth"
KID = "notification-test-key"
# The smallest size the verifier accepts; generated per run, never stored.
TEST_KEY_SIZE_BITS = 2048

# A DSN that points nowhere: the database of a test comes from the container.
PLACEHOLDER_DSN = "postgresql+asyncpg://notification:secret@localhost:5432/notification"

# Everything a test can write. Truncated around the tests that commit for real;
# the rest are isolated by a rollback and need no cleaning.
WRITTEN_TABLES = (
    "notifications",
    "recipients",
    "telegram_link_codes",
    "outbox",
    "processed_events",
)

RecipientFactory = Callable[..., Awaitable[Recipient]]
# Builds the Authorization header of a caller, optionally with a given user id.
AuthorizationFactory = Callable[..., dict[str, str]]


def build_settings(**overrides: object) -> NotificationSettings:
    """Settings of the service under test, with no environment behind them."""
    fields: dict[str, object] = {
        "environment": Environment.TEST,
        "database_dsn": PLACEHOLDER_DSN,
        "redis_dsn": "redis://localhost:6379/0",
        "kafka_bootstrap_servers": "localhost:9092",
        "jwt_issuer": ISSUER,
        "jwks_url": "http://auth:8001",
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


@pytest.fixture(scope="session")
def signing_key() -> rsa.RSAPrivateKey:
    """The throwaway key this run signs its tokens with."""
    return rsa.generate_private_key(public_exponent=65537, key_size=TEST_KEY_SIZE_BITS)


def build_app(
    settings: NotificationSettings,
    session_factory: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
) -> FastAPI:
    """The real application, wired to the database of the test.

    The lifespan does not run, so what it would set up is done here: sessions
    come from the given factory and the verifier trusts the suite's key.
    """
    application = create_application(settings)

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_get_session
    use_authentication(
        application,
        TokenVerifier(
            keys=StaticKeys.from_pem(kid=KID, public_pem=public_pem(signing_key)),
            issuer=ISSUER,
        ),
    )
    return application


@pytest.fixture
def app(
    settings: NotificationSettings,
    session_factory: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
) -> Iterator[FastAPI]:
    """The application over sessions whose writes are rolled back."""
    application = build_app(settings, session_factory, signing_key)

    yield application

    application.dependency_overrides.clear()


@pytest.fixture
def telegram_app(
    session_factory: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
) -> Iterator[FastAPI]:
    """The application of a platform that has a bot."""
    settings = build_settings(
        telegram_bot_token=SecretStr("123:token"), telegram_bot_username="barber_bot"
    )
    application = build_app(settings, session_factory, signing_key)

    yield application

    application.dependency_overrides.clear()


def public_pem(key: rsa.RSAPrivateKey) -> str:
    """The public half of a key, in the form the verifier reads."""
    return (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )


def mint(key: rsa.RSAPrivateKey, *, subject: str, token_type: str) -> str:
    """Sign one token the way ``auth`` would, with ``kid`` and ``typ``."""
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "sub": subject,
        "typ": token_type,
        "iss": ISSUER,
        "jti": str(uuid4()),
        "iat": now,
        "exp": now + timedelta(minutes=15),
        "roles": [{"role": "client", "salon_id": None}],
    }
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": KID})


@pytest.fixture
def authorize(signing_key: rsa.RSAPrivateKey) -> AuthorizationFactory:
    """A user token as a bearer header."""

    def factory(*, user_id: UUID | None = None) -> dict[str, str]:
        token = mint(signing_key, subject=str(user_id or uuid4()), token_type=ACCESS_TOKEN_TYPE)
        return {"Authorization": f"Bearer {token}"}

    return factory


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
