"""Codes for linking a Telegram chat."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from barber_notification.models.telegram_link_code import TelegramLinkCode

__all__ = ["TelegramLinkRepository"]


class TelegramLinkRepository:
    """Access to ``telegram_link_codes``. Never commits."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, *, code_hash: str, user_id: UUID, expires_at: datetime) -> None:
        self._session.add(
            TelegramLinkCode(code_hash=code_hash, user_id=user_id, expires_at=expires_at)
        )
        await self._session.flush()

    async def claim(self, code_hash: str, *, now: datetime) -> UUID | None:
        """Spend the code and name its owner; nothing for a spent or stale one.

        One statement: two messages with the same code cannot both find it
        unused.
        """
        statement = (
            update(TelegramLinkCode)
            .where(
                TelegramLinkCode.code_hash == code_hash,
                TelegramLinkCode.used_at.is_(None),
                TelegramLinkCode.expires_at > now,
            )
            .values(used_at=now)
            .returning(TelegramLinkCode.user_id)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()
