"""Fixtures of the booking suite.

The database is created once per session and brought up by the real
migrations. Isolation is a rollback: every test gets sessions bound to one
connection with an open transaction, and whatever it writes disappears when it
ends.

Tokens are real and really verified: the suite signs its own with a throwaway
RSA key and points the production verifier at the public half.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from barber_booking.main import create_application
from barber_booking.models.booking import Booking
from barber_booking.models.master_settings import MasterSettings
from barber_booking.settings import ALEMBIC_INI, BookingSettings
from barber_common.auth import ACCESS_TOKEN_TYPE, StaticKeys, TokenVerifier, use_authentication
from barber_common.cache import Cache, cache_from_dsn
from barber_common.config import Environment
from barber_common.db import create_engine, get_session
from barber_common.db.session import transaction
from barber_common.testing.fixtures import (
    apply_migrations,
    create_database,
    isolated_session_factory,
)

DATABASE_NAME = "booking_test"

# A DSN that points nowhere: the database of a test comes from the container.
PLACEHOLDER_DSN = "postgresql+asyncpg://booking:secret@localhost:5432/booking"

ISSUER = "https://barber.local/auth"
KID = "booking-test-key"
# The smallest size the verifier accepts; generated per run, never stored.
TEST_KEY_SIZE_BITS = 2048

MOSCOW = "Europe/Moscow"

MasterSettingsFactory = Callable[..., Awaitable[MasterSettings]]
BookingFactory = Callable[..., Awaitable[Booking]]
# Builds the Authorization header of a caller holding the given roles.
AuthorizationFactory = Callable[..., dict[str, str]]


def build_settings(**overrides: object) -> BookingSettings:
    """Settings of the service under test, with no environment behind them."""
    fields: dict[str, object] = {
        "environment": Environment.TEST,
        "database_dsn": PLACEHOLDER_DSN,
        "redis_dsn": "redis://localhost:6379/0",
        "kafka_bootstrap_servers": "localhost:9092",
        "jwt_issuer": ISSUER,
        "jwks_url": "http://auth:8001",
        "catalog_url": "http://catalog:8002",
        "auth_url": "http://auth:8001",
        "service_client_secret": "test-secret",
    }
    fields.update(overrides)
    return BookingSettings(**fields)  # type: ignore[arg-type]  # settings fields are typed per key


@pytest.fixture(scope="session")
def settings() -> BookingSettings:
    return build_settings()


@pytest.fixture(scope="session")
def signing_key() -> rsa.RSAPrivateKey:
    """The throwaway key this run signs its tokens with."""
    return rsa.generate_private_key(public_exponent=65537, key_size=TEST_KEY_SIZE_BITS)


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
def app(
    settings: BookingSettings,
    session_factory: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
) -> Iterator[FastAPI]:
    """The real application, wired to the rolled back database of the test.

    The lifespan does not run, so what it would set up is done here: sessions
    come from the isolated factory and the verifier trusts the suite's key.
    ``app.state.cache`` stays unset, which the dependency reads as a disabled
    cache.
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


def mint(
    key: rsa.RSAPrivateKey,
    *,
    subject: str,
    token_type: str,
    roles: tuple[tuple[str, UUID | None], ...] = (),
) -> str:
    """Sign one token the way ``auth`` would, with ``kid`` and ``typ``."""
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "sub": subject,
        "typ": token_type,
        "iss": ISSUER,
        "jti": str(uuid4()),
        "iat": now,
        "exp": now + timedelta(minutes=15),
    }
    if roles:
        claims["roles"] = [
            {"role": role, "salon_id": str(salon_id) if salon_id else None}
            for role, salon_id in roles
        ]
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": KID})


@pytest.fixture
def authorize(signing_key: rsa.RSAPrivateKey) -> AuthorizationFactory:
    """A user token with the roles a test needs, as a bearer header."""

    def factory(
        *,
        roles: tuple[tuple[str, UUID | None], ...] = (("client", None),),
        user_id: UUID | None = None,
    ) -> dict[str, str]:
        token = mint(
            signing_key,
            subject=str(user_id or uuid4()),
            token_type=ACCESS_TOKEN_TYPE,
            roles=roles,
        )
        return {"Authorization": f"Bearer {token}"}

    return factory


@pytest.fixture
async def cache(redis_dsn: str) -> AsyncIterator[Cache]:
    """A real Redis, emptied first so counters cannot leak between tests."""
    client = cache_from_dsn(redis_dsn, timeout_seconds=1.0)
    await client.flushdb()
    built = Cache(client, ttl_seconds=60)

    yield built

    await built.aclose()


@pytest.fixture
def unreachable_cache() -> Cache:
    """A cache pointed at a port nothing listens on."""
    return Cache(cache_from_dsn("redis://127.0.0.1:1/0", timeout_seconds=0.05), ttl_seconds=60)


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
