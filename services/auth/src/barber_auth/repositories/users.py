"""Users and their roles."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from barber_auth.domain.identifiers import SalonId, UserId
from barber_auth.domain.roles import Role
from barber_auth.models.user import User
from barber_auth.models.user_role import UserRole

__all__ = ["UserRepository"]


class UserRepository:
    """Access to ``users`` and ``user_roles``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, user: User) -> User:
        """Stage a new user inside the caller's transaction.

        The flush is what turns a unique violation into an exception here,
        where the scenario can still tell which contact collided, instead of at
        the commit where all it would know is that something did.
        """
        self._session.add(user)
        await self._session.flush()
        return user

    async def grant_role(
        self,
        *,
        user_id: UserId,
        role: Role,
        salon_id: SalonId | None = None,
    ) -> UserRole:
        """Give a user a role, globally or inside one salon."""
        grant = UserRole(user_id=user_id, role=role.value, salon_id=salon_id)
        self._session.add(grant)
        await self._session.flush()
        return grant

    async def get_by_id(self, user_id: UserId) -> User | None:
        """One user by identifier."""
        return await self._session.get(User, user_id)

    async def get_by_email(self, email: str) -> User | None:
        """One user by normalised email.

        ``lower()`` on both sides, matching the functional unique index: a
        comparison against the raw column would miss the row the index
        considers a duplicate.
        """
        statement = select(User).where(func.lower(User.email) == email.lower())
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def get_by_phone(self, phone: str) -> User | None:
        """One user by normalised E.164 phone."""
        result = await self._session.execute(select(User).where(User.phone == phone))
        return result.scalar_one_or_none()

    async def roles_of(self, user_id: UserId) -> Sequence[UserRole]:
        """Every grant of one user, global and scoped alike."""
        statement = (
            select(UserRole).where(UserRole.user_id == user_id).order_by(UserRole.created_at)
        )
        result = await self._session.execute(statement)
        return result.scalars().all()

    async def mark_email_confirmed(self, *, user: User, confirmed_at: datetime) -> None:
        """Stamp the moment the address was confirmed.

        Deliberately not a boolean: the moment answers questions a flag cannot,
        and a second confirmation is prevented by the token, not by the column.
        """
        user.email_confirmed_at = confirmed_at
        await self._session.flush()
