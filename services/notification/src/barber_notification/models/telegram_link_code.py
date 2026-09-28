"""One-time codes that link a Telegram chat to an account."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from barber_common.db.base import Base

__all__ = ["TelegramLinkCode"]


class TelegramLinkCode(Base):
    """A code handed to a user and sent back to the bot from their chat.

    Only the SHA-256 of the code is kept: a copy of this table links nobody's
    chat. Used once, then kept with ``used_at`` until it is pruned.
    """

    __tablename__ = "telegram_link_codes"

    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[UUID]
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (Index("ix_telegram_link_codes_user_id", "user_id"),)
