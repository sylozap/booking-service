"""What a master offers, and what they charge for it."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from barber_common.db.base import Base

if TYPE_CHECKING:
    from barber_catalog.models.master import Master
    from barber_catalog.models.service import Service

__all__ = ["MasterService"]


class MasterService(Base):
    """The link between one master and one service of their salon.

    Price and duration overrides are optional and independent; final figures
    are computed in :mod:`barber_catalog.domain.pricing`.

    Unlinking sets ``is_active`` to false instead of deleting the row, so the
    overrides are kept. Reads treat an inactive link as missing. The foreign key
    to ``services`` has no cascade, since services are never deleted.
    """

    __tablename__ = "master_services"

    master_id: Mapped[UUID] = mapped_column(
        ForeignKey("masters.id", ondelete="CASCADE"), primary_key=True
    )
    service_id: Mapped[UUID] = mapped_column(ForeignKey("services.id"), primary_key=True)

    price_override: Mapped[Decimal | None] = mapped_column(default=None)
    duration_override: Mapped[int | None] = mapped_column(default=None)

    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    master: Mapped[Master] = relationship(back_populates="offerings", lazy="raise")
    service: Mapped[Service] = relationship(lazy="raise")

    __table_args__ = (
        CheckConstraint(
            "duration_override IS NULL OR duration_override > 0",
            name="duration_override_positive",
        ),
        CheckConstraint(
            "price_override IS NULL OR price_override >= 0",
            name="price_override_not_negative",
        ),
    )
