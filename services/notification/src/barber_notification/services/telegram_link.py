"""Linking a Telegram chat to an account, and unlinking it.

The user asks for a code while signed in, then opens the bot with it
(``t.me/<bot>?start=<code>``). Telegram sends ``/start <code>`` from their
chat, and that chat becomes their Telegram address. The code proves the chat
belongs to whoever held the session: it is random, short-lived, single-use and
stored only as a hash.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from barber_common.db.session import transaction
from barber_common.errors import DomainError
from barber_common.logging import get_logger
from barber_notification.repositories.recipients import RecipientRepository
from barber_notification.repositories.telegram_links import TelegramLinkRepository

__all__ = [
    "IssueTelegramLinkCode",
    "LinkTelegramChat",
    "TelegramLinkCodeIssued",
    "TelegramNotConfigured",
    "UnlinkTelegram",
    "hash_code",
]

_logger = get_logger(__name__)

Clock = Callable[[], datetime]

# 18 random bytes, 24 URL-safe characters: fits the 64 characters Telegram
# allows in a start parameter, and cannot be guessed within its lifetime.
_CODE_BYTES = 18


def _utc_now() -> datetime:
    return datetime.now(UTC)


def hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


class TelegramNotConfigured(DomainError):
    """This deployment has no bot, so there is nothing to link a chat to."""

    code = "channel_unavailable"
    http_status = 422
    title = "Channel is not available"


@dataclass(frozen=True, slots=True)
class TelegramLinkCodeIssued:
    """What the user gets: the code, and the link that carries it to the bot."""

    code: str
    link: str
    expires_at: datetime


class IssueTelegramLinkCode:
    """Hand a signed-in user a code to send to the bot."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        bot_username: str | None,
        ttl: timedelta,
        clock: Clock = _utc_now,
    ) -> None:
        self._session = session
        self._links = TelegramLinkRepository(session)
        self._bot_username = bot_username
        self._ttl = ttl
        self._clock = clock

    async def execute(self, user_id: UUID) -> TelegramLinkCodeIssued:
        if not self._bot_username:
            raise TelegramNotConfigured("Telegram is not configured for this platform")

        code = secrets.token_urlsafe(_CODE_BYTES)
        expires_at = self._clock() + self._ttl
        async with transaction(self._session):
            await self._links.add(code_hash=hash_code(code), user_id=user_id, expires_at=expires_at)

        _logger.info("telegram link code issued", user_id=str(user_id))
        return TelegramLinkCodeIssued(
            code=code,
            link=f"https://t.me/{self._bot_username}?start={code}",
            expires_at=expires_at,
        )


class LinkTelegramChat:
    """Make the chat a code came from the Telegram address of its owner."""

    def __init__(self, session: AsyncSession, *, clock: Clock = _utc_now) -> None:
        self._session = session
        self._links = TelegramLinkRepository(session)
        self._recipients = RecipientRepository(session)
        self._clock = clock

    async def execute(self, *, code: str, chat_id: int) -> UUID | None:
        """The user now reachable in this chat, or nothing for a bad code.

        The recipient row may not exist yet -- the registration event can still
        be on its way -- so it is created, and the contacts arrive later.
        """
        async with transaction(self._session):
            user_id = await self._links.claim(hash_code(code), now=self._clock())
            if user_id is None:
                return None
            recipient = await self._recipients.lock_or_create(user_id)
            await self._recipients.save(replace(recipient, telegram_chat_id=chat_id))

        _logger.info("telegram chat linked", user_id=str(user_id))
        return user_id


class UnlinkTelegram:
    """Stop sending to the user's Telegram. Safe to repeat."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._recipients = RecipientRepository(session)

    async def execute(self, user_id: UUID) -> None:
        async with transaction(self._session):
            await self._recipients.forget_telegram_chat(user_id)
        _logger.info("telegram chat unlinked", user_id=str(user_id))
