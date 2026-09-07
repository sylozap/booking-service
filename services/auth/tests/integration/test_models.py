"""What the auth schema refuses to store, checked against a real PostgreSQL.

These are constraints, not code: a rule enforced by a check in a scenario is a
rule the next write path forgets. The assertions here go through the database.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from barber_auth.domain.identifiers import SalonId, UserId
from barber_auth.domain.roles import Role
from barber_auth.models.user import User
from barber_auth.models.user_role import UserRole
from barber_auth.repositories.users import UserRepository
from barber_common.db.errors import constraint_name_of

pytestmark = pytest.mark.integration


def user_factory(
    *,
    email: str = "ivan@example.com",
    phone: str = "+79991234567",
    password_hash: str = "$argon2id$v=19$m=8,t=1,p=1$c2FsdHNhbHQ$aGFzaA",
) -> User:
    """An unsaved user. The test names what its assertion is about."""
    return User(email=email, phone=phone, password_hash=password_hash)


async def test_the_same_address_in_another_case_is_rejected_by_the_database(
    session: AsyncSession,
) -> None:
    users = UserRepository(session)
    await users.add(user_factory(email="ivan@example.com"))

    with pytest.raises(IntegrityError) as failure:
        await users.add(user_factory(email="IVAN@Example.com", phone="+79991234568"))

    # Uniqueness is on lower(email): two cases of one address are one person.
    assert constraint_name_of(failure.value) == "uq_users_lower_email"


async def test_the_same_phone_number_is_rejected_by_the_database(
    session: AsyncSession,
) -> None:
    users = UserRepository(session)
    await users.add(user_factory(phone="+79991234567"))

    with pytest.raises(IntegrityError) as failure:
        await users.add(user_factory(email="petr@example.com", phone="+79991234567"))

    assert constraint_name_of(failure.value) == "uq_users_phone"


async def test_a_global_role_and_a_salon_role_coexist_for_one_user(
    session: AsyncSession,
) -> None:
    users = UserRepository(session)
    user = await users.add(user_factory())
    user_id = UserId(user.id)

    await users.grant_role(user_id=user_id, role=Role.CLIENT)
    await users.grant_role(user_id=user_id, role=Role.SALON_ADMIN, salon_id=SalonId(uuid4()))
    await users.grant_role(user_id=user_id, role=Role.SALON_ADMIN, salon_id=SalonId(uuid4()))

    # Three grants: the identity of a role is the pair with its scope, and a
    # single primary key over (user_id, role, salon_id) could not express it.
    assert len(await users.roles_of(user_id)) == 3


async def test_the_same_global_role_cannot_be_granted_twice(session: AsyncSession) -> None:
    users = UserRepository(session)
    user_id = UserId((await users.add(user_factory())).id)
    await users.grant_role(user_id=user_id, role=Role.CLIENT)

    with pytest.raises(IntegrityError) as failure:
        await users.grant_role(user_id=user_id, role=Role.CLIENT)

    assert constraint_name_of(failure.value) == "uq_user_roles_user_id_role_global"


async def test_the_same_role_in_the_same_salon_cannot_be_granted_twice(
    session: AsyncSession,
) -> None:
    users = UserRepository(session)
    user_id = UserId((await users.add(user_factory())).id)
    salon_id = SalonId(uuid4())
    await users.grant_role(user_id=user_id, role=Role.SALON_ADMIN, salon_id=salon_id)

    with pytest.raises(IntegrityError) as failure:
        await users.grant_role(user_id=user_id, role=Role.SALON_ADMIN, salon_id=salon_id)

    assert constraint_name_of(failure.value) == "uq_user_roles_user_id_role_salon_id"


async def test_a_role_outside_the_catalogue_is_rejected(session: AsyncSession) -> None:
    users = UserRepository(session)
    user_id = UserId((await users.add(user_factory())).id)

    session.add(UserRole(user_id=user_id, role="owner"))
    with pytest.raises(IntegrityError) as failure:
        await session.flush()

    assert constraint_name_of(failure.value) == "ck_user_roles_role"


async def test_a_new_user_starts_unconfirmed_and_active(session: AsyncSession) -> None:
    user = await UserRepository(session).add(user_factory())

    await session.refresh(user)

    assert user.email_confirmed_at is None
    assert user.is_active is True
