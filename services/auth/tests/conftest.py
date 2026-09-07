"""Fixtures of the auth suite.

The database is created once per session and brought up by the real
migrations -- the same scripts the migration Job runs. Nothing here builds a
schema from the models: a schema that never went through Alembic leaves the
migrations untested until the first deployment.

Isolation is a rollback. Every test gets sessions bound to one connection with
an open transaction, and whatever it writes disappears when it ends, so two
tests writing to ``users`` never see each other.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from barber_auth.adapters.argon2_hasher import Argon2Hasher
from barber_auth.adapters.database_keys import DatabaseKeys
from barber_auth.adapters.dev_mailer import DevMailer
from barber_auth.adapters.rsa_signer import RsaTokenSigner
from barber_auth.domain.identifiers import SalonId, UserId
from barber_auth.domain.roles import Role
from barber_auth.main import create_application
from barber_auth.models.user import User
from barber_auth.repositories.users import UserRepository
from barber_auth.services.keys import RegisterSigningKey
from barber_auth.services.tokens import IssueTokenPair
from barber_auth.settings import ALEMBIC_INI, AuthSettings
from barber_common.auth import TokenVerifier, use_authentication
from barber_common.config import Environment
from barber_common.db import create_engine, get_session
from barber_common.db.session import create_session_factory, transaction
from barber_common.testing.fixtures import (
    apply_migrations,
    create_database,
    isolated_session_factory,
)

DATABASE_NAME = "auth_test"

# A DSN that points nowhere: the tests that do not touch a database get their
# settings from here, and the ones that do override it with the container.
PLACEHOLDER_DSN = "postgresql+asyncpg://auth:secret@localhost:5432/auth"


# 2048 bits and not the 4096 of scripts/gen_keys.py: this is the smallest size
# the signer accepts, generating it is roughly ten times faster, and no test
# asserts anything about the key size. Generated per session, never stored --
# a PEM committed to the repository would be a signing key in git history and
# would trip scripts/check-secrets.sh, which is exactly the point of that check.
TEST_KEY_SIZE_BITS = 2048


def generate_private_key_pem(key_size: int = TEST_KEY_SIZE_BITS) -> str:
    """A throwaway RSA private key in PEM, for the tests that need to sign."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def build_settings(**overrides: object) -> AuthSettings:
    """Settings of the service under test, with no environment behind them.

    argon2 is deliberately cheap here. The production parameters spend 64 MiB
    and tens of milliseconds per hash, and a suite that registers users would
    pay that on every test for a property none of them assert.

    A signing key is generated unless the caller passes one. It is the one
    field with no usable default in production, so every test that builds
    settings would otherwise have to know about it.
    """
    fields: dict[str, object] = {
        "environment": Environment.TEST,
        "database_dsn": PLACEHOLDER_DSN,
        "redis_dsn": "redis://localhost:6379/0",
        "kafka_bootstrap_servers": "localhost:9092",
        "password_argon2_time_cost": 1,
        "password_argon2_memory_kib": 8,
        "password_argon2_parallelism": 1,
    }
    if "jwt_private_key" not in overrides and "jwt_private_key_path" not in overrides:
        fields["jwt_private_key"] = generate_private_key_pem()
    fields.update(overrides)
    return AuthSettings(**fields)  # type: ignore[arg-type]  # settings fields are typed per key


@pytest.fixture(scope="session")
def settings() -> AuthSettings:
    return build_settings()


@pytest.fixture(scope="session")
def auth_dsn(postgres_dsn: str) -> str:
    """A database of its own for this suite, migrated to head."""
    dsn = create_database(postgres_dsn, DATABASE_NAME)
    apply_migrations(dsn=dsn, alembic_ini=ALEMBIC_INI)
    return dsn


@pytest.fixture
async def engine(auth_dsn: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(auth_dsn, pool_size=5, max_overflow=5)

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


@pytest.fixture(scope="session")
def make_settings() -> Callable[..., AuthSettings]:
    """The settings factory itself, for tests about configuration.

    Handed over as a fixture rather than imported: pytest runs this suite in
    importlib mode, so ``tests.conftest`` is not an importable module, and a
    second copy of the factory in a test file would drift from this one.
    """
    return build_settings


@pytest.fixture(scope="session")
def make_private_key_pem() -> Callable[[], str]:
    """The key generator, for tests that need more than one key."""
    return generate_private_key_pem


@pytest.fixture(scope="session")
def private_key_pem(settings: AuthSettings) -> str:
    """The key the application under test signs with, as a PEM."""
    return settings.signing_key_pem()


@pytest.fixture(scope="session")
def other_private_key_pem() -> str:
    """A second key, for the tests about telling two keys apart."""
    return generate_private_key_pem()


@pytest.fixture(scope="session")
def signer(settings: AuthSettings) -> RsaTokenSigner:
    """The signing key of the application under test.

    Session scoped, like the settings it is built from: generating an RSA key
    costs a noticeable fraction of a second and nothing in the suite benefits
    from a different key per test.
    """
    return RsaTokenSigner(settings.signing_key_pem())


@pytest.fixture
async def concurrent_session_factory(
    auth_dsn: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions on genuinely separate connections, committing for real.

    The ordinary ``session_factory`` puts every session on one connection
    inside one transaction and rolls it back at the end. That is the right
    trade for almost everything and useless for the one thing it cannot show:
    two requests contending for the same row. ``SELECT ... FOR UPDATE`` on a
    single connection blocks against itself or sees its own uncommitted work,
    so the rotation race (T1.7) needs real connections and real commits.

    The price is that a test using this fixture cleans up after itself -- see
    the users it creates being deleted in ``tests/integration/test_refresh_race.py``.
    """
    engine = create_engine(auth_dsn, pool_size=5, max_overflow=5)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


@pytest.fixture
def hasher(settings: AuthSettings) -> Argon2Hasher:
    return Argon2Hasher(settings)


@pytest.fixture
def mailer(settings: AuthSettings) -> DevMailer:
    return DevMailer(
        environment=settings.environment,
        confirmation_url=settings.email_confirmation_url,
    )


@pytest.fixture
def app(
    settings: AuthSettings,
    session_factory: async_sessionmaker[AsyncSession],
) -> Iterator[FastAPI]:
    """The real application, wired to the rolled back database of the test.

    The lifespan does not run -- there is no broker and no schema check here --
    so the session dependency is pointed at the isolated factory instead of at
    ``app.state.database``. Everything else is production code: the routers,
    the middleware, the error handlers and the scenarios behind them.
    """
    application = create_application(settings)

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_get_session

    yield application

    application.dependency_overrides.clear()


@pytest.fixture
async def registered_signing_key(
    session: AsyncSession,
    signer: RsaTokenSigner,
) -> str:
    """Publish the key of the application under test, as the lifespan would.

    The ``app`` fixture does not run the lifespan -- there is no broker and no
    schema check in a test -- so the registration that normally happens at
    startup is done here, through the same scenario. A test that reads JWKS or
    verifies a token asks for this fixture; one that does not is unaffected.
    """
    return await RegisterSigningKey(session=session, signer=signer).execute()


# What a test gets to build a user with: it names the fields its assertion is
# about and the factory fills in the rest.
UserFactory = Callable[..., Awaitable[User]]


@pytest.fixture
async def make_user(session: AsyncSession, hasher: Argon2Hasher) -> UserFactory:
    """Create an account directly, without going through registration.

    Registration is a scenario with its own tests. A login test that had to
    drive it would fail when registration changes, for reasons that have
    nothing to do with logging in -- and it could not build the accounts that
    matter most here, the unconfirmed and the deactivated ones.
    """

    async def factory(
        *,
        email: str = "ivan@example.com",
        phone: str = "+79991234567",
        password: str = "correct-horse-9",
        confirmed: bool = True,
        is_active: bool = True,
        roles: tuple[tuple[Role, SalonId | None], ...] = ((Role.CLIENT, None),),
    ) -> User:
        users = UserRepository(session)
        async with transaction(session):
            user = await users.add(
                User(
                    email=email,
                    phone=phone,
                    password_hash=hasher.hash(password),
                    email_confirmed_at=datetime.now(UTC) if confirmed else None,
                    is_active=is_active,
                )
            )
            for role, salon_id in roles:
                await users.grant_role(user_id=UserId(user.id), role=role, salon_id=salon_id)
        return user

    return factory


@pytest.fixture
def app_with_verifier(
    app: FastAPI,
    session_factory: async_sessionmaker[AsyncSession],
    settings: AuthSettings,
) -> Iterator[FastAPI]:
    """The application with token verification wired to the test database.

    ``create_application`` installs the verifier in the lifespan, which does
    not run here, so the same wiring is done against the rolled-back session
    factory. Everything else is production code: the tokens are real, signed by
    the real key and checked by the real verifier, so a test cannot accidentally
    grant itself a role by handing the endpoint a dictionary.
    """
    use_authentication(
        app,
        TokenVerifier(
            keys=DatabaseKeys(session_factory),
            issuer=settings.jwt_issuer,
            leeway_seconds=settings.jwt_leeway_seconds,
        ),
    )

    yield app

    app.state.token_verifier = None


# Signs a user in and returns the Authorization header of their session.
AuthorizationFactory = Callable[..., Awaitable[dict[str, str]]]


@pytest.fixture
async def authorize(
    session: AsyncSession,
    make_user: UserFactory,
    signer: RsaTokenSigner,
    settings: AuthSettings,
    registered_signing_key: str,
) -> AuthorizationFactory:
    """Build a caller with the roles a test needs, and their bearer header.

    Goes through the real login scenario rather than minting a token by hand:
    a test that forges its own tokens stops testing whether the endpoint checks
    them.
    """

    async def factory(
        *,
        roles: tuple[tuple[Role, SalonId | None], ...] = ((Role.CLIENT, None),),
        email: str | None = None,
        phone: str | None = None,
    ) -> dict[str, str]:
        address = email or f"caller-{uuid4().hex[:12]}@example.com"
        password = "correct-horse-9"
        await make_user(
            email=address,
            phone=phone or f"+7999{uuid4().int % 10**7:07d}",
            password=password,
            roles=roles,
        )
        pair = await IssueTokenPair(
            session=session,
            hasher=hasher_of(settings),
            signer=signer,
            issuer=settings.jwt_issuer,
            access_ttl_minutes=settings.access_token_ttl_minutes,
            refresh_ttl_days=settings.refresh_token_ttl_days,
        ).execute(email=address, password=password)
        return {"Authorization": f"Bearer {pair.access_token}"}

    return factory


def hasher_of(settings: AuthSettings) -> Argon2Hasher:
    """One hasher per settings object, built where a fixture cannot reach."""
    return Argon2Hasher(settings)
