"""The master profile: who a master is, not when they work."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import Boolean, ForeignKey, Index, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from barber_common.db.base import Base

if TYPE_CHECKING:
    from barber_catalog.models.master_service import MasterService

__all__ = ["Master"]


class Master(Base):
    """One master, inside one salon.

    The profile and offered services live here; the buffer and weekly schedule
    live in ``booking`` under the same identifier. ``user_id`` names an account
    in ``auth`` and is neither a foreign key nor verified.
    """

    __tablename__ = "masters"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    salon_id: Mapped[UUID] = mapped_column(ForeignKey("salons.id", ondelete="CASCADE"))
    user_id: Mapped[UUID]

    display_name: Mapped[str] = mapped_column(String(200))
    bio: Mapped[str | None] = mapped_column(Text, default=None)
    photo_url: Mapped[str | None] = mapped_column(String(1000), default=None)
    specialization: Mapped[str | None] = mapped_column(String(200), default=None)

    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    # ``lazy="raise"``: callers load this explicitly with ``selectinload``.
    offerings: Mapped[list[MasterService]] = relationship(
        back_populates="master",
        lazy="raise",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        # One profile per account per salon. The same person may be a master in
        # two salons, and that is two profiles, not one.
        Index("uq_masters_user_id_salon_id", "user_id", "salon_id", unique=True),
        # The masters of one salon, optionally only the active ones.
        Index("ix_masters_salon_id_is_active", "salon_id", "is_active"),
    )
