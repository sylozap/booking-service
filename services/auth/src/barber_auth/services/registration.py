"""Registration: the account, its role, its confirmation token and two events.

All of them are written in one transaction.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from barber_auth.adapters.dev_mailer import DevMailer
from barber_auth.domain.contacts import normalize_email, normalize_phone
from barber_auth.domain.errors import EmailAlreadyRegistered, PhoneAlreadyRegistered
from barber_auth.domain.identifiers import UserId
from barber_auth.domain.passwords import PasswordHasher, check_password_policy
from barber_auth.domain.roles import Role
from barber_auth.models.user import User
from barber_auth.repositories.users import UserRepository
from barber_auth.services.email_confirmation import IssueEmailConfirmation
from barber_common.db.errors import (
    SQLSTATE_UNIQUE_VIOLATION,
    constraint_name_of,
    sqlstate_of,
)
from barber_common.db.session import transaction
from barber_common.events.users import (
    AUTH_USERS_TOPIC,
    USER_AGGREGATE_TYPE,
    UserEventType,
    UserRegistered,
)
from barber_common.logging import get_logger
from barber_common.outbox.repository import OutboxRepository

__all__ = ["RegisterUser", "RegisteredUser"]

_logger = get_logger(__name__)

# Names of the unique indexes of ``users``, as PostgreSQL reports them in a
# 23505. Two registrations of one address can pass the pre-check at the same
# time, and then the database is the one that decides which of them collided.
_UNIQUE_EMAIL_INDEX = "uq_users_lower_email"
_UNIQUE_PHONE_INDEX = "uq_users_phone"


@dataclass(frozen=True, slots=True)
class RegisteredUser:
    """What the caller gets back. The password and the token are not here."""

    user_id: UserId
    email: str
    phone: str
    role: Role


class RegisterUser:
    """Create an account with the ``client`` role and ask for a confirmation."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        hasher: PasswordHasher,
        mailer: DevMailer,
        confirmation_ttl_hours: int,
        password_min_length: int,
    ) -> None:
        self._session = session
        self._users = UserRepository(session)
        self._outbox = OutboxRepository(session)
        self._issue_confirmation = IssueEmailConfirmation(
            session=session, ttl_hours=confirmation_ttl_hours
        )
        self._hasher = hasher
        self._mailer = mailer
        self._password_min_length = password_min_length

    async def execute(self, *, email: str, phone: str, password: str) -> RegisteredUser:
        """Register one user."""
        normalized_email = normalize_email(email)
        normalized_phone = normalize_phone(phone)
        check_password_policy(password, min_length=self._password_min_length)

        # argon2 holds the CPU for tens of milliseconds, so it runs in a thread
        # and before the transaction opens.
        password_hash = await asyncio.to_thread(self._hasher.hash, password)

        async with transaction(self._session):
            await self._reject_taken_contacts(email=normalized_email, phone=normalized_phone)

            user = await self._add_user(
                email=normalized_email,
                phone=normalized_phone,
                password_hash=password_hash,
            )
            user_id = UserId(user.id)
            await self._users.grant_role(user_id=user_id, role=Role.CLIENT)

            await self._outbox.add(
                topic=AUTH_USERS_TOPIC,
                aggregate_type=USER_AGGREGATE_TYPE,
                aggregate_id=user_id,
                event_type=UserEventType.REGISTERED.value,
                payload=UserRegistered(
                    user_id=user_id,
                    email=user.email,
                    phone=user.phone,
                    email_confirmed=False,
                ),
            )
            issued = await self._issue_confirmation.execute(user_id=user_id, email=user.email)

            registered = RegisteredUser(
                user_id=user_id,
                email=user.email,
                phone=user.phone,
                role=Role.CLIENT,
            )

        # Outside the transaction and after the commit: the letter is a side
        # effect of a registration that happened, and a mailer that fails must
        # not undo an account that exists.
        self._mailer.send_confirmation(email=registered.email, token=issued.token)

        # Only the user id is logged, never the email or phone.
        _logger.info("user registered", user_id=str(registered.user_id))
        return registered

    async def _reject_taken_contacts(self, *, email: str, phone: str) -> None:
        """Answer a duplicate with the contact that is taken.

        Checked before the insert, because the unique violation does not say
        which of the two contacts collided.
        """
        if await self._users.get_by_email(email) is not None:
            raise EmailAlreadyRegistered("This email is already registered")
        if await self._users.get_by_phone(phone) is not None:
            raise PhoneAlreadyRegistered("This phone number is already registered")

    async def _add_user(self, *, email: str, phone: str, password_hash: str) -> User:
        """Insert the row, translating the race the pre-check cannot win."""
        try:
            return await self._users.add(
                User(email=email, phone=phone, password_hash=password_hash)
            )
        except IntegrityError as error:
            raise _translate_unique_violation(error) from error


def _translate_unique_violation(error: IntegrityError) -> Exception:
    """Turn a 23505 on ``users`` into the domain error that names the contact.

    Any other error is returned unchanged.
    """
    if sqlstate_of(error) != SQLSTATE_UNIQUE_VIOLATION:
        return error

    constraint = constraint_name_of(error)
    if constraint == _UNIQUE_EMAIL_INDEX:
        return EmailAlreadyRegistered("This email is already registered")
    if constraint == _UNIQUE_PHONE_INDEX:
        return PhoneAlreadyRegistered("This phone number is already registered")
    return error
