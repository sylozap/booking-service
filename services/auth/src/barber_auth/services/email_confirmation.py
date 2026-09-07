"""Issuing and spending the one-time email confirmation token."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from barber_auth.domain.confirmation import (
    CONFIRMATION_TOKEN_BYTES,
    hash_confirmation_token,
    is_confirmation_usable,
)
from barber_auth.domain.errors import ConfirmationTokenInvalid
from barber_auth.domain.identifiers import UserId
from barber_auth.repositories.email_confirmations import EmailConfirmationRepository
from barber_auth.repositories.users import UserRepository
from barber_common.db.session import transaction
from barber_common.events.users import (
    AUTH_USERS_TOPIC,
    USER_AGGREGATE_TYPE,
    UserEmailConfirmationRequested,
    UserEmailConfirmed,
    UserEventType,
)
from barber_common.logging import get_logger
from barber_common.outbox.repository import OutboxRepository

__all__ = ["ConfirmEmail", "ConfirmedEmail", "IssueEmailConfirmation", "IssuedConfirmation"]

_logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class IssuedConfirmation:
    """The token that has to reach the address, and when it stops working.

    The token is returned rather than stored: the database keeps only its hash,
    so this is the one moment the plain value exists, and it exists only long
    enough to be handed to the mailer.
    """

    token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ConfirmedEmail:
    """Who was confirmed, for the response and for the log."""

    user_id: UserId
    email: str


class IssueEmailConfirmation:
    """Create a confirmation token and queue the letter that carries it.

    Called from the registration and, later, from "send me the link again".
    Writes inside the caller's transaction: the token row and the event have to
    appear together with the user, or not at all.
    """

    def __init__(self, *, session: AsyncSession, ttl_hours: int) -> None:
        self._confirmations = EmailConfirmationRepository(session)
        self._outbox = OutboxRepository(session)
        self._ttl = timedelta(hours=ttl_hours)

    async def execute(
        self,
        *,
        user_id: UserId,
        email: str,
        now: datetime | None = None,
    ) -> IssuedConfirmation:
        """Issue one token for one address."""
        issued_at = now or datetime.now(UTC)
        expires_at = issued_at + self._ttl

        # secrets, not uuid4: this is a credential, and the point is that it
        # cannot be guessed from another one issued a moment earlier.
        token = secrets.token_urlsafe(CONFIRMATION_TOKEN_BYTES)
        await self._confirmations.add(
            user_id=user_id,
            token_hash=hash_confirmation_token(token),
            expires_at=expires_at,
        )

        await self._outbox.add(
            topic=AUTH_USERS_TOPIC,
            aggregate_type=USER_AGGREGATE_TYPE,
            aggregate_id=user_id,
            event_type=UserEventType.EMAIL_CONFIRMATION_REQUESTED.value,
            payload=UserEmailConfirmationRequested(
                user_id=user_id,
                email=email,
                token=token,
                expires_at=expires_at.isoformat(),
            ),
        )

        # The token is deliberately absent from this record.
        _logger.info("email confirmation issued", user_id=str(user_id))
        return IssuedConfirmation(token=token, expires_at=expires_at)


class ConfirmEmail:
    """Spend a confirmation token and mark the address as confirmed."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._confirmations = EmailConfirmationRepository(session)
        self._users = UserRepository(session)
        self._outbox = OutboxRepository(session)

    async def execute(self, *, token: str, now: datetime | None = None) -> ConfirmedEmail:
        """Confirm the address the token was issued for.

        Unknown, already used and expired all answer with the same error: three
        different answers would let a caller learn which tokens exist.
        """
        confirmed_at = now or datetime.now(UTC)
        token_hash = hash_confirmation_token(token)

        async with transaction(self._session):
            confirmation = await self._confirmations.get_by_token_hash(token_hash)
            if confirmation is None:
                raise ConfirmationTokenInvalid("Confirmation token is unknown or already spent")

            if not is_confirmation_usable(
                used_at=confirmation.used_at,
                expires_at=confirmation.expires_at,
                now=confirmed_at,
            ):
                raise ConfirmationTokenInvalid("Confirmation token is unknown or already spent")

            user = await self._users.get_by_id(UserId(confirmation.user_id))
            if user is None:  # pragma: no cover - the foreign key guarantees it
                raise ConfirmationTokenInvalid("Confirmation token is unknown or already spent")

            await self._confirmations.mark_used(confirmation=confirmation, used_at=confirmed_at)
            # Confirming twice is prevented by the token, so a second call
            # never gets here and the first stamp is never overwritten.
            if user.email_confirmed_at is None:
                await self._users.mark_email_confirmed(user=user, confirmed_at=confirmed_at)

            await self._outbox.add(
                topic=AUTH_USERS_TOPIC,
                aggregate_type=USER_AGGREGATE_TYPE,
                aggregate_id=user.id,
                event_type=UserEventType.EMAIL_CONFIRMED.value,
                payload=UserEmailConfirmed(user_id=user.id, email=user.email),
            )

            result = ConfirmedEmail(user_id=UserId(user.id), email=user.email)

        _logger.info("email confirmed", user_id=str(result.user_id))
        return result
