"""A service a salon offers: a haircut, a shave, a beard trim."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from barber_common.db.base import Base

__all__ = ["Service"]

# ISO 4217, three letters. Stored next to the amount rather than assumed: an
# amount without its currency is a number, not money
# (docs/CODING_STANDARDS.md section 3).
CURRENCY_LENGTH = 3


class Service(Base):
    """One offering of a salon, with the base duration and the base price.

    "Base" because a master may charge differently for the same service, and
    the final figures are ``COALESCE(override, base)`` -- see
    :mod:`barber_catalog.domain.pricing`.

    **A service is never deleted.** Bookings snapshot the name and the price at
    the moment they are made, but they still carry ``service_id``, and a row
    that disappears turns the history into dangling identifiers. Withdrawing an
    offering is ``is_archived``: it leaves the listings and stays reachable by
    its own identifier (T2.4).

    ``base_price`` is ``numeric(10, 2)`` through the type map of the chassis.
    Money is never a floating point type.
    """

    __tablename__ = "services"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    salon_id: Mapped[UUID] = mapped_column(ForeignKey("salons.id", ondelete="CASCADE"))

    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, default=None)

    base_duration_min: Mapped[int]
    base_price: Mapped[Decimal]
    currency: Mapped[str] = mapped_column(String(CURRENCY_LENGTH))

    is_archived: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        # Named by docs/05-data-model.md. A service of zero minutes would make
        # every slot on the grid free at once.
        CheckConstraint("base_duration_min > 0", name="base_duration_min_positive"),
        CheckConstraint("base_price >= 0", name="base_price_not_negative"),
        # The listing of T2.4: the services of one salon, archived ones left
        # out, ordered by the keyset ``(name, id)``.
        Index("ix_services_salon_id_is_archived_name_id", "salon_id", "is_archived", "name", "id"),
    )
