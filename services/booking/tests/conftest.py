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
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from barber_booking.clients.catalog import CatalogClient
from barber_booking.clients.service_token import ServiceTokenProvider
from barber_booking.main import create_application
from barber_booking.models.booking import Booking
from barber_booking.models.master_settings import MasterSettings
from barber_booking.models.schedule_exception import ScheduleException
from barber_booking.models.schedule_template import ScheduleTemplate
from barber_booking.settings import ALEMBIC_INI, BookingSettings
from barber_common.auth import ACCESS_TOKEN_TYPE, StaticKeys, TokenVerifier, use_authentication
from barber_common.cache import Cache, cache_from_dsn
from barber_common.config import Environment
from barber_common.db import Database, create_engine, get_session
from barber_common.db.session import transaction
from barber_common.http import ServiceClient
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

# Everything a test can write. Truncated around the tests that commit for real;
# the rest are isolated by a rollback and need no cleaning.
WRITTEN_TABLES = (
    "bookings",
    "schedule_exceptions",
    "schedule_templates",
    "master_settings",
    "idempotency_keys",
    "outbox",
    "processed_events",
)

MasterSettingsFactory = Callable[..., Awaitable[MasterSettings]]
BookingFactory = Callable[..., Awaitable[Booking]]
TemplateFactory = Callable[..., Awaitable[ScheduleTemplate]]
ExceptionFactory = Callable[..., Awaitable[ScheduleException]]
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


class FakeCatalog:
    """``catalog`` and ``auth`` as the booking service sees them.

    A transport rather than a patched client: everything between the scenario
    and the wire -- the token, the retries, the breaker, the contract -- is the
    code that runs in production.
    """

    def __init__(self) -> None:
        self.salon_id = uuid4()
        self.master_active = True
        self.duration_min = 45
        self.timezone = MOSCOW
        self.slot_step_min = 15
        self.booking_min_lead_min = 120
        self.booking_horizon_days = 60
        self.cancel_deadline_min = 240
        self.service_name = "Haircut"
        self.price = "3500.00"
        self.currency = "RUB"
        # What the next read of an offering answers: 200 unless a test says
        # otherwise. "gone" makes catalog unreachable.
        self.answers: str = "offering"
        self.calls = 0

    def client(self) -> CatalogClient:
        transport = httpx.MockTransport(self._handle)
        return CatalogClient(
            http=ServiceClient(base_url="http://catalog", upstream="catalog", transport=transport),
            tokens=ServiceTokenProvider(
                http=ServiceClient(base_url="http://auth", upstream="auth", transport=transport),
                client_id="booking",
                client_secret=SecretStr("secret"),
            ),
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/v1/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "token",
                    "token_type": "Bearer",
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                    "scopes": ["catalog:read"],
                },
            )

        self.calls += 1
        if self.answers == "gone":
            raise httpx.ConnectError("connection refused")
        if self.answers == "not_offered":
            return httpx.Response(
                404,
                json={"status": 404, "code": "not_found", "detail": "no link"},
                headers={"content-type": "application/problem+json"},
            )

        master_id, service_id = request.url.path.split("/")[-3], request.url.path.split("/")[-1]
        return httpx.Response(200, json=self._offering(master_id, service_id))

    def _offering(self, master_id: str, service_id: str) -> dict[str, object]:
        return {
            "master_id": master_id,
            "salon_id": str(self.salon_id),
            "master_active": self.master_active,
            "service_id": service_id,
            "service_name": self.service_name,
            "duration_min": self.duration_min,
            "price": self.price,
            "currency": self.currency,
            "salon": {
                "timezone": self.timezone,
                "slot_step_min": self.slot_step_min,
                "booking_min_lead_min": self.booking_min_lead_min,
                "booking_horizon_days": self.booking_horizon_days,
                "cancel_deadline_min": self.cancel_deadline_min,
            },
        }


@pytest.fixture
def catalog() -> FakeCatalog:
    """The catalog this test answers with."""
    return FakeCatalog()


def build_app(
    settings: BookingSettings,
    session_factory: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
    catalog: FakeCatalog,
) -> FastAPI:
    """The real application, wired to the database of the test.

    The lifespan does not run, so what it would set up is done here: sessions
    come from the given factory, the verifier trusts the suite's key and the
    catalog is the fake one. ``app.state.cache`` stays unset, which the
    dependency reads as a disabled cache.
    """
    application = create_application(settings)

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_get_session
    application.state.catalog = catalog.client()
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
    settings: BookingSettings,
    session_factory: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
    catalog: FakeCatalog,
) -> Iterator[FastAPI]:
    """The application over sessions whose writes are rolled back."""
    application = build_app(settings, session_factory, signing_key, catalog)

    yield application

    application.dependency_overrides.clear()


@pytest.fixture
async def concurrent_session_factory(
    booking_dsn: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions on connections of their own, cleaned up by truncating.

    Savepoints on one connection are not two transactions, so anything about
    what happens when two requests run side by side needs these.
    """
    engine = create_engine(booking_dsn, pool_size=25, max_overflow=5)
    await _truncate(engine)

    yield Database(engine).session_factory

    await _truncate(engine)
    await engine.dispose()


@pytest.fixture
async def concurrent_session(
    concurrent_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """One session that really commits."""
    async with concurrent_session_factory() as session:
        yield session


@pytest.fixture
def concurrent_app(
    settings: BookingSettings,
    concurrent_session_factory: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
    catalog: FakeCatalog,
) -> Iterator[FastAPI]:
    """The application over real connections, for tests about concurrency."""
    application = build_app(settings, concurrent_session_factory, signing_key, catalog)

    yield application

    application.dependency_overrides.clear()


async def _truncate(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {', '.join(WRITTEN_TABLES)} CASCADE"))


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
async def make_template(session: AsyncSession) -> TemplateFactory:
    """Add one line of a weekly template, in the salon's local time."""

    async def factory(
        *,
        master_id: UUID,
        weekday: int,
        start_time: time = time(10),
        end_time: time = time(20),
        valid_from: date = date(2020, 1, 1),
        valid_to: date | None = None,
    ) -> ScheduleTemplate:
        line = ScheduleTemplate(
            master_id=master_id,
            weekday=weekday,
            start_time=start_time,
            end_time=end_time,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        async with transaction(session):
            session.add(line)
            await session.flush()
        return line

    return factory


@pytest.fixture
async def make_exception(session: AsyncSession) -> ExceptionFactory:
    """Add one exception of one date."""

    async def factory(
        *,
        master_id: UUID,
        effective_on: date,
        kind: str,
        start_time: time | None = None,
        end_time: time | None = None,
        reason: str | None = None,
    ) -> ScheduleException:
        exception = ScheduleException(
            master_id=master_id,
            effective_on=effective_on,
            kind=kind,
            start_time=start_time,
            end_time=end_time,
            reason=reason,
        )
        async with transaction(session):
            session.add(exception)
            await session.flush()
        return exception

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
        client_user_id: UUID | None = None,
        cancel_deadline_min: int = 240,
    ) -> Booking:
        client_id = client_user_id or uuid4()
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
            cancel_deadline_min=cancel_deadline_min,
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
