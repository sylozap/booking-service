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

    Both overrides are optional and independent: a master may charge more for
    the same forty-five minutes, or need an hour at the salon's price, or
    differ in both, or in neither. The final figures are
    ``COALESCE(override, base)``, computed in
    :mod:`barber_catalog.domain.pricing` and nowhere else -- the master card of
    T2.3 and the internal endpoint of T2.6 are two callers of the same rule.

    **Unlinking is a flag, not a delete.** ``DELETE`` on the endpoint of T2.5
    sets ``is_active`` to false. The row is what carries the overrides, and
    dropping it would mean a master who stops offering a service for a month
    has to have their prices entered again; it also makes the endpoint safe to
    repeat, which a delete of an absent row is not. Every read -- the card, the
    internal endpoint -- filters on the flag, so an inactive link is invisible
    exactly like a missing one.

    The foreign key to ``services`` deliberately has no cascade: a service is
    never deleted (see :class:`~barber_catalog.models.service.Service`), and a
    cascade here would be a promise about something that must not happen.
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
