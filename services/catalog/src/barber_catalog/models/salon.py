"""The salon: the shop window entry, and the policies bookings obey."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import Boolean, CheckConstraint, Index, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column, validates

from barber_common.db.base import Base

__all__ = ["Salon"]

# Defaults of docs/02-domain-rules.md. They live in the database rather than in
# the application so that a salon created by a seed script or by hand in psql
# gets the same policies as one created through the API.
DEFAULT_SLOT_STEP_MIN = 15
DEFAULT_BOOKING_MIN_LEAD_MIN = 120
DEFAULT_BOOKING_HORIZON_DAYS = 60
DEFAULT_CANCEL_DEADLINE_MIN = 240


class Salon(Base):
    """One salon, with the four policies that decide when it can be booked.

    The policies are stored here and applied in ``booking``
    (docs/02-domain-rules.md): the salon owns the rule, the service that holds
    the bookings enforces it, and the internal endpoint of T2.6 is how the
    second learns the first.

    ``timezone`` is an IANA identifier -- ``Europe/Moscow`` -- and never an
    offset. An offset is correct for half the year, and a weekly schedule
    stored against one silently moves by an hour on the day the country changes
    its clocks.
    """

    __tablename__ = "salons"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, default=None)
    address: Mapped[str] = mapped_column(String(500))
    city: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(16))

    timezone: Mapped[str] = mapped_column(String(64))

    slot_step_min: Mapped[int] = mapped_column(server_default=text(str(DEFAULT_SLOT_STEP_MIN)))
    booking_min_lead_min: Mapped[int] = mapped_column(
        server_default=text(str(DEFAULT_BOOKING_MIN_LEAD_MIN))
    )
    booking_horizon_days: Mapped[int] = mapped_column(
        server_default=text(str(DEFAULT_BOOKING_HORIZON_DAYS))
    )
    cancel_deadline_min: Mapped[int] = mapped_column(
        server_default=text(str(DEFAULT_CANCEL_DEADLINE_MIN))
    )

    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("slot_step_min > 0", name="slot_step_min_positive"),
        CheckConstraint("booking_min_lead_min >= 0", name="booking_min_lead_min_not_negative"),
        CheckConstraint("booking_horizon_days > 0", name="booking_horizon_days_positive"),
        CheckConstraint("cancel_deadline_min >= 0", name="cancel_deadline_min_not_negative"),
        # The listing of T2.2: filtered by city, ordered by the keyset
        # ``(name, id)``. There is no second index for the unfiltered listing
        # -- the platform has twenty salons (docs/03-services.md), and sorting
        # twenty rows is cheaper than maintaining an index over them.
        Index("ix_salons_city_name_id", "city", "name", "id"),
    )

    @validates("timezone")
    def _validate_timezone(self, _key: str, value: str) -> str:
        """Refuse a zone the system cannot resolve.

        Here rather than only in the request schema because this is the one
        point every write path crosses -- the API, a seed script, a future
        consumer. PostgreSQL cannot check it: there is no constraint that knows
        the IANA database.

        A plain ``ValueError``, not a domain error: models may not import the
        domain or the chassis errors (docs/CODING_STANDARDS.md section 2.2).
        The request schema runs the same check first and answers ``422``, so a
        caller never sees this one -- it is the backstop, not the message.
        """
        try:
            ZoneInfo(value)
        except (ValueError, KeyError) as error:
            raise ValueError(f"{value!r} is not a known IANA time zone") from error
        return value
