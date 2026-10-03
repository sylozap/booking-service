"""The first administrator, created from configuration at startup."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from pydantic import SecretStr
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from barber_auth.adapters.argon2_hasher import Argon2Hasher
from barber_auth.domain.errors import WeakPassword
from barber_auth.domain.identifiers import UserId
from barber_auth.domain.roles import Role
from barber_auth.models.user import User
from barber_auth.repositories.users import UserRepository
from barber_auth.services.bootstrap import EnsureBootstrapAdmin
from barber_auth.settings import BootstrapAdminConfig
from barber_common.db.session import transaction
from barber_common.outbox.models import OutboxMessage

pytestmark = pytest.mark.integration

ADMIN = BootstrapAdminConfig(
    email="Admin@Barber.Example",
    phone="+79990000001",
    password=SecretStr("admin-password-of-the-stand-7"),
)


def ensure(
    session: AsyncSession, hasher: Argon2Hasher, admin: BootstrapAdminConfig | None = ADMIN
) -> EnsureBootstrapAdmin:
    return EnsureBootstrapAdmin(session=session, hasher=hasher, admin=admin, password_min_length=10)


async def roles_of(session: AsyncSession, user_id: UserId) -> set[str]:
    async with transaction(session):
        return {grant.role for grant in await UserRepository(session).roles_of(user_id)}


async def users_named(session: AsyncSession, email: str) -> list[User]:
    async with transaction(session):
        statement = select(User).where(func.lower(User.email) == email.lower())
        return list((await session.execute(statement)).scalars().all())


async def test_creates_a_confirmed_super_admin_who_can_also_book(
    session: AsyncSession, hasher: Argon2Hasher
) -> None:
    user_id = await ensure(session, hasher).execute()

    assert user_id is not None
    [user] = await users_named(session, ADMIN.email)
    assert user.email == "admin@barber.example"
    assert user.email_confirmed_at is not None
    assert hasher.verify(ADMIN.password.get_secret_value(), user.password_hash)
    assert await roles_of(session, user_id) == {Role.CLIENT.value, Role.SUPER_ADMIN.value}


async def test_tells_notification_the_address_is_confirmed(
    session: AsyncSession, hasher: Argon2Hasher
) -> None:
    user_id = await ensure(session, hasher).execute()

    async with transaction(session):
        [event] = (await session.execute(select(OutboxMessage))).scalars().all()

    assert event.event_type == "user.registered"
    assert event.aggregate_id == user_id
    assert event.payload["email_confirmed"] is True


async def test_a_second_start_changes_nothing(session: AsyncSession, hasher: Argon2Hasher) -> None:
    first = await ensure(session, hasher).execute()
    [before] = await users_named(session, ADMIN.email)
    password_hash = before.password_hash

    second = await ensure(session, hasher).execute()

    async with transaction(session):
        events = (await session.execute(select(func.count(OutboxMessage.id)))).scalar_one()
    [after] = await users_named(session, ADMIN.email)
    assert second == first
    assert after.password_hash == password_hash
    assert events == 1


async def test_an_existing_account_gets_the_role_and_keeps_its_password(
    session: AsyncSession,
    hasher: Argon2Hasher,
    make_user: Callable[..., Awaitable[User]],
) -> None:
    existing = await make_user(
        email="admin@barber.example", phone="+79990000002", password="own-password-9"
    )

    user_id = await ensure(session, hasher).execute()

    [user] = await users_named(session, ADMIN.email)
    assert user_id == existing.id
    assert hasher.verify("own-password-9", user.password_hash)
    assert Role.SUPER_ADMIN.value in await roles_of(session, user_id)


async def test_nothing_is_created_without_configuration(
    session: AsyncSession, hasher: Argon2Hasher
) -> None:
    user_id = await ensure(session, hasher, admin=None).execute()

    async with transaction(session):
        users = (await session.execute(select(func.count(User.id)))).scalar_one()
    assert user_id is None
    assert users == 0


async def test_a_weak_password_stops_the_start(session: AsyncSession, hasher: Argon2Hasher) -> None:
    weak = ADMIN.model_copy(update={"password": SecretStr("short")})

    with pytest.raises(WeakPassword):
        await ensure(session, hasher, admin=weak).execute()


async def test_two_replicas_starting_together_create_one_account(
    concurrent_session_factory: async_sessionmaker[AsyncSession], hasher: Argon2Hasher
) -> None:
    async def start() -> UserId | None:
        async with concurrent_session_factory() as session:
            return await ensure(session, hasher).execute()

    try:
        first, second = await asyncio.gather(start(), start())

        async with concurrent_session_factory() as session:
            users = await users_named(session, ADMIN.email)
            assert first == second
            assert len(users) == 1
            assert Role.SUPER_ADMIN.value in await roles_of(session, UserId(users[0].id))
    finally:
        # These sessions commit, so the rows are removed by hand.
        async with concurrent_session_factory() as session, transaction(session):
            created = select(User.id).where(func.lower(User.email) == ADMIN.email.lower())
            await session.execute(
                delete(OutboxMessage).where(OutboxMessage.aggregate_id.in_(created))
            )
            await session.execute(delete(User).where(User.id.in_(created)))
