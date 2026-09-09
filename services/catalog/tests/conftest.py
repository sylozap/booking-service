"""Fixtures of the catalog suite.

The database is created once per session and brought up by the real
migrations -- the same scripts the migration Job runs. Nothing here builds a
schema from the models: a schema that never went through Alembic leaves the
migrations untested until the first deployment.

Isolation is a rollback. Every test gets sessions bound to one connection with
an open transaction, and whatever it writes disappears when it ends, so two
tests writing to ``salons`` never see each other.

**Tokens are real and really verified.** ``catalog`` does not issue tokens --
that is what ``auth`` is for -- so the suite signs its own with a throwaway RSA
key and points the production verifier at the matching public half. Everything
between the ``Authorization`` header and the endpoint is the code that runs in
production; a test that handed a router a hand-built principal would stop
testing whether the router checks anything at all.
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

from barber_catalog.main import create_application
from barber_catalog.models.master import Master
from barber_catalog.models.master_service import MasterService
from barber_catalog.models.salon import Salon
from barber_catalog.models.service import Service
from barber_catalog.settings import ALEMBIC_INI, CatalogSettings
from barber_common.auth import (
    ACCESS_TOKEN_TYPE,
    SERVICE_TOKEN_TYPE,
    StaticKeys,
    TokenVerifier,
    use_authentication,
)
from barber_common.cache import Cache, cache_from_dsn
from barber_common.config import Environment
from barber_common.db import create_engine, get_session
from barber_common.db.session import transaction
from barber_common.testing.fixtures import (
    apply_migrations,
    create_database,
    isolated_session_factory,
)

DATABASE_NAME = "catalog_test"

# A DSN that points nowhere: the tests that do not touch a database get their
# settings from here, and the ones that do override it with the container.
PLACEHOLDER_DSN = "postgresql+asyncpg://catalog:secret@localhost:5432/catalog"

ISSUER = "https://barber.local/auth"
KID = "catalog-test-key"

# 2048 bits: the smallest size the verifier accepts, roughly ten times faster
# to generate than the 4096 of scripts/gen_keys.py, and no test asserts
# anything about the key size. Generated per run and never stored -- a PEM in
# the repository would trip scripts/check-secrets.sh, which is the point of it.
TEST_KEY_SIZE_BITS = 2048

MOSCOW = "Europe/Moscow"


def build_settings(**overrides: object) -> CatalogSettings:
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
    return CatalogSettings(**fields)  # type: ignore[arg-type]  # settings fields are typed per key


@pytest.fixture(scope="session")
def settings() -> CatalogSettings:
    return build_settings()


@pytest.fixture(scope="session")
def make_settings() -> Callable[..., CatalogSettings]:
    """The settings factory itself, for tests about configuration.

    Handed over as a fixture rather than imported: pytest runs this suite in
    importlib mode, so ``tests.conftest`` is not an importable module, and a
    second copy of the factory in a test file would drift from this one.
    """
    return build_settings


@pytest.fixture(scope="session")
def signing_key() -> rsa.RSAPrivateKey:
    """The throwaway key this run signs its tokens with."""
    return rsa.generate_private_key(public_exponent=65537, key_size=TEST_KEY_SIZE_BITS)


@pytest.fixture(scope="session")
def catalog_dsn(postgres_dsn: str) -> str:
    """A database of its own for this suite, migrated to head."""
    dsn = create_database(postgres_dsn, DATABASE_NAME)
    apply_migrations(dsn=dsn, alembic_ini=ALEMBIC_INI)
    return dsn


@pytest.fixture
async def engine(catalog_dsn: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(catalog_dsn, pool_size=5, max_overflow=5)

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
    """One session for a test that talks to the repositories directly."""
    async with session_factory() as session:
        yield session


@pytest.fixture
def app(
    settings: CatalogSettings,
    session_factory: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
) -> Iterator[FastAPI]:
    """The real application, wired to the rolled back database of the test.

    The lifespan does not run here -- there is no broker, no schema check and
    no ``auth`` to fetch keys from -- so the two things it would set up are
    done directly: the session dependency is pointed at the isolated factory,
    and the verifier is given the public half of the suite's key instead of a
    JWKS client. Everything else is production code: the routers, the
    middleware, the error handlers and the scenarios behind them.
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


@pytest.fixture
async def cache(redis_dsn: str) -> AsyncIterator[Cache]:
    """A real Redis, emptied before the test so keys cannot leak between them.

    The generation counter is what makes that necessary: a counter left behind
    by a previous test would put this one in a namespace whose contents it did
    not write, which is harmless in production and confusing in a test.
    """
    client = cache_from_dsn(redis_dsn, timeout_seconds=1.0)
    await client.flushdb()
    built = Cache(client, ttl_seconds=60)

    yield built

    await built.aclose()


@pytest.fixture
def unreachable_cache() -> Cache:
    """A cache pointed at a port nothing is listening on.

    Port 1 rather than a hostname that does not resolve: a bad name fails
    differently on different machines, while a closed port refuses the
    connection everywhere.
    """
    return Cache(cache_from_dsn("redis://127.0.0.1:1/0", timeout_seconds=0.05), ttl_seconds=60)


@pytest.fixture
def app_with_cache(app: FastAPI, cache: Cache) -> Iterator[FastAPI]:
    """The application reading and writing a real cache.

    The plain ``app`` fixture leaves ``app.state.cache`` unset, which the
    dependency reads as a disabled cache -- so every other test in this suite
    exercises the uncached path, and the caching is proven only where it is the
    subject.
    """
    app.state.cache = cache

    yield app

    app.state.cache = None


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
    scopes: tuple[str, ...] = (),
    ttl_minutes: int = 15,
) -> str:
    """Sign one token the way ``auth`` would.

    The shape is the contract of docs/04-api-contracts.md, not an invention of
    the tests: ``kid`` in the JOSE header so the consumer picks a key before
    verifying anything, ``typ`` telling a user token from a service one.
    """
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "sub": subject,
        "typ": token_type,
        "iss": ISSUER,
        "jti": str(uuid4()),
        "iat": now,
        "exp": now + timedelta(minutes=ttl_minutes),
    }
    if roles:
        claims["roles"] = [
            {"role": role, "salon_id": str(salon_id) if salon_id else None}
            for role, salon_id in roles
        ]
    if scopes:
        claims["scopes"] = list(scopes)

    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": KID})


# Builds the Authorization header of a caller holding the given roles.
AuthorizationFactory = Callable[..., dict[str, str]]


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
def authorize_service(signing_key: rsa.RSAPrivateKey) -> AuthorizationFactory:
    """A machine-to-machine token, as another service would present it."""

    def factory(
        *, scopes: tuple[str, ...] = ("catalog:read",), client_id: str = "booking"
    ) -> dict[str, str]:
        token = mint(
            signing_key,
            subject=client_id,
            token_type=SERVICE_TOKEN_TYPE,
            scopes=scopes,
        )
        return {"Authorization": f"Bearer {token}"}

    return factory


SalonFactory = Callable[..., Awaitable[Salon]]
MasterFactory = Callable[..., Awaitable[Master]]
ServiceFactory = Callable[..., Awaitable[Service]]
OfferingFactory = Callable[..., Awaitable[MasterService]]


@pytest.fixture
async def make_salon(session: AsyncSession) -> SalonFactory:
    """Create a salon directly, without going through the API.

    A factory rather than a ready-made object: a test names the two or three
    fields its assertion is about and everything else gets a valid default.
    """

    async def factory(
        *,
        name: str = "Barbershop One",
        city: str = "Moscow",
        timezone: str = MOSCOW,
        slot_step_min: int = 15,
        booking_min_lead_min: int = 120,
        booking_horizon_days: int = 60,
        cancel_deadline_min: int = 240,
        is_active: bool = True,
    ) -> Salon:
        salon = Salon(
            name=name,
            description=None,
            address="Tverskaya 1",
            city=city,
            phone="+74951234567",
            timezone=timezone,
            slot_step_min=slot_step_min,
            booking_min_lead_min=booking_min_lead_min,
            booking_horizon_days=booking_horizon_days,
            cancel_deadline_min=cancel_deadline_min,
            is_active=is_active,
        )
        async with transaction(session):
            session.add(salon)
            await session.flush()
        return salon

    return factory


@pytest.fixture
async def make_master(session: AsyncSession) -> MasterFactory:
    """Create a master profile directly."""

    async def factory(
        *,
        salon_id: UUID,
        user_id: UUID | None = None,
        display_name: str = "Ivan",
        specialization: str | None = "Barber",
        is_active: bool = True,
    ) -> Master:
        master = Master(
            salon_id=salon_id,
            user_id=user_id or uuid4(),
            display_name=display_name,
            specialization=specialization,
            is_active=is_active,
        )
        async with transaction(session):
            session.add(master)
            await session.flush()
        return master

    return factory


@pytest.fixture
async def make_service(session: AsyncSession) -> ServiceFactory:
    """Create a salon service directly."""

    async def factory(
        *,
        salon_id: UUID,
        name: str = "Haircut",
        base_duration_min: int = 45,
        base_price: Decimal = Decimal("3500.00"),
        currency: str = "RUB",
        is_archived: bool = False,
    ) -> Service:
        service = Service(
            salon_id=salon_id,
            name=name,
            base_duration_min=base_duration_min,
            base_price=base_price,
            currency=currency,
            is_archived=is_archived,
        )
        async with transaction(session):
            session.add(service)
            await session.flush()
        return service

    return factory


@pytest.fixture
async def make_offering(session: AsyncSession) -> OfferingFactory:
    """Link a master to a service, with or without overrides."""

    async def factory(
        *,
        master_id: UUID,
        service_id: UUID,
        price_override: Decimal | None = None,
        duration_override: int | None = None,
        is_active: bool = True,
    ) -> MasterService:
        offering = MasterService(
            master_id=master_id,
            service_id=service_id,
            price_override=price_override,
            duration_override=duration_override,
            is_active=is_active,
        )
        async with transaction(session):
            session.add(offering)
            await session.flush()
        return offering

    return factory
