"""One-time tokens confirming an email address."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from barber_common.db.base import Base

__all__ = ["EmailConfirmation"]


class EmailConfirmation(Base):
    """One issued confirmation link.

    Single use and short lived: ``used_at`` is stamped instead of the row being
    deleted, so a second click on the same link is a token that was used rather
    than a token that never existed, and the background cleanup of T1.12 has
    something to prune.
    """

    __tablename__ = "email_confirmations"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    token_hash: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))

    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None] = mapped_column(default=None)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        Index("uq_email_confirmations_token_hash", "token_hash", unique=True),
        Index("ix_email_confirmations_user_id", "user_id"),
    )
