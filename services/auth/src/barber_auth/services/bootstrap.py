"""The first administrator of the platform, created from configuration.

Only a ``super_admin`` creates a salon, and only a ``super_admin`` grants that
role, so the first one cannot come through the API: a platform started empty
would have nobody able to do anything. The deployment names the account in its
Secret, and auth makes sure it exists at every start.

Configuration rather than a migration, for the reason the service clients are:
a password hash in a migration is a secret in git.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from barber_auth.domain.contacts import normalize_email, normalize_phone
from barber_auth.domain.identifiers import UserId
from barber_auth.domain.passwords import PasswordHasher, check_password_policy
from barber_auth.domain.roles import Role
from barber_auth.models.user import User
from barber_auth.repositories.users import UserRepository
from barber_auth.settings import BootstrapAdminConfig
from barber_common.db.errors import SQLSTATE_UNIQUE_VIOLATION, sqlstate_of
from barber_common.db.session import transaction
from barber_common.events.users import (
    AUTH_USERS_TOPIC,
    USER_AGGREGATE_TYPE,
    UserEventType,
    UserRegistered,
)
from barber_common.logging import get_logger
from barber_common.outbox.repository import OutboxRepository

__all__ = ["EnsureBootstrapAdmin"]

_logger = get_logger(__name__)

# Two replicas starting together both find no account and both insert it; the
# one that loses the unique index reads the winner's row on its second pass.
_ATTEMPTS = 2


class EnsureBootstrapAdmin:
    """Create the configured administrator, or give the existing one the role.

    An account that exists keeps its password, contacts and confirmation:
    whoever runs the platform may have changed them since, and a restart must
    not undo that. Only the role is ensured -- the one thing the configuration
    promises.
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        hasher: PasswordHasher,
        admin: BootstrapAdminConfig | None,
        password_min_length: int,
    ) -> None:
        self._session = session
        self._users = UserRepository(session)
        self._outbox = OutboxRepository(session)
        self._hasher = hasher
        self._admin = admin
        self._password_min_length = password_min_length

    async def execute(self, *, now: datetime | None = None) -> UserId | None:
        """Make sure the administrator exists. Returns its id, or None if none is configured."""
        if self._admin is None:
            return None

        email = normalize_email(self._admin.email)
        phone = normalize_phone(self._admin.phone)
        password = self._admin.password.get_secret_value()
        # A weak password is refused here as it would be at registration: the
        # most powerful account of the platform is no place for an exception.
        check_password_policy(password, min_length=self._password_min_length)

        for attempt in range(1, _ATTEMPTS + 1):
            try:
                user_id = await self._ensure(
                    email=email, phone=phone, password=password, now=now or datetime.now(UTC)
                )
            except IntegrityError as error:
                if sqlstate_of(error) != SQLSTATE_UNIQUE_VIOLATION or attempt == _ATTEMPTS:
                    raise
                continue
            # The id and nothing else: the address is personal data.
            _logger.info("bootstrap administrator ensured", user_id=str(user_id))
            return user_id

        raise AssertionError("unreachable")  # pragma: no cover - the loop returns or raises

    async def _ensure(self, *, email: str, phone: str, password: str, now: datetime) -> UserId:
        async with transaction(self._session):
            existing = await self._users.get_by_email(email)
        if existing is not None:
            await self._grant_missing_role(UserId(existing.id))
            return UserId(existing.id)

        # argon2 outside the transaction, as at registration.
        password_hash = await asyncio.to_thread(self._hasher.hash, password)

        async with transaction(self._session):
            user = await self._users.add(
                User(
                    email=email,
                    phone=phone,
                    password_hash=password_hash,
                    # Nobody would receive the letter: the address is the
                    # operator's own, confirmed by putting it in the Secret.
                    email_confirmed_at=now,
                )
            )
            user_id = UserId(user.id)
            # client as every account has, so the administrator can also book.
            await self._users.grant_role(user_id=user_id, role=Role.CLIENT)
            await self._users.grant_role(user_id=user_id, role=Role.SUPER_ADMIN)
            await self._outbox.add(
                topic=AUTH_USERS_TOPIC,
                aggregate_type=USER_AGGREGATE_TYPE,
                aggregate_id=user_id,
                event_type=UserEventType.REGISTERED.value,
                payload=UserRegistered(
                    user_id=user_id,
                    email=user.email,
                    phone=user.phone,
                    email_confirmed=True,
                ),
            )
        return user_id

    async def _grant_missing_role(self, user_id: UserId) -> None:
        async with transaction(self._session):
            roles = await self._users.roles_of(user_id)
            if not any(r.role == Role.SUPER_ADMIN.value and r.salon_id is None for r in roles):
                await self._users.grant_role(user_id=user_id, role=Role.SUPER_ADMIN)
