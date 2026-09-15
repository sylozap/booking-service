"""Refresh tokens: one row per issued token, hashed."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import Mapped, mapped_column

from barber_common.db.base import Base

__all__ = ["RefreshToken"]


class RefreshToken(Base):
    """One refresh token of one user.

    Only the hash is stored. ``family_id`` links every rotated token to its
    predecessors, so reuse of a spent token revokes the whole family.
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    token_hash: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    family_id: Mapped[UUID]

    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)
    replaced_by: Mapped[UUID | None] = mapped_column(default=None)

    user_agent: Mapped[str | None] = mapped_column(String(255), default=None)
    ip: Mapped[str | None] = mapped_column(INET, default=None)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        Index("uq_refresh_tokens_token_hash", "token_hash", unique=True),
        # The query behind "log out everywhere": every live token of one user.
        Index("ix_refresh_tokens_user_id_revoked_at", "user_id", "revoked_at"),
    )
