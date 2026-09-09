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

    The master is split across two services by "who they are" against "when
    they work" (docs/03-services.md): the profile and the offerings live here,
    the buffer and the weekly schedule live in ``booking``, keyed by the same
    identifier. ``is_active`` is the source of truth for both, which is why
    deactivating one (T2.8) publishes an event rather than only writing a row.

    ``user_id`` points at an account in ``auth`` and carries no foreign key: no
    constraint of this database may reach into another service. Whether the
    account exists is not checked here either -- a cross-service validation of
    a profile field does not pay for itself, and a profile naming an account
    that was never created is a data error rather than a broken system (T2.3).
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

    # ``lazy="raise"`` is the project-wide rule (docs/CODING_STANDARDS.md
    # section 8): the card of T2.3 and the internal endpoint of T2.6 load this
    # with an explicit ``selectinload``, and anything that forgets to gets an
    # exception instead of an N+1 that only shows up under load.
    offerings: Mapped[list[MasterService]] = relationship(
        back_populates="master",
        lazy="raise",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        # One profile per account per salon. The same person may be a master in
        # two salons, and that is two profiles, not one.
        Index("uq_masters_user_id_salon_id", "user_id", "salon_id", unique=True),
        # The listing of T2.3: the masters of one salon, optionally only the
        # active ones. Named by docs/05-data-model.md.
        Index("ix_masters_salon_id_is_active", "salon_id", "is_active"),
    )
