"""A booking: one client, one master, one service, one span of time.

The table holds the main invariant of the platform. Two active bookings of one
master never overlap, and the database refuses the second one with ``23P01``
whatever path wrote it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import TSTZRANGE, ExcludeConstraint, Range
from sqlalchemy.orm import Mapped, mapped_column

from barber_common.db.base import Base

__all__ = [
    "ACTIVE_BOOKING_STATUSES",
    "BOOKING_STATUSES",
    "OVERLAP_CONSTRAINT",
    "Booking",
    "occupied_range_of",
]

# The two lists as SQL spells them. Models do not import the domain, so these
# are literals, and a unit test holds them equal to the domain's enum.
BOOKING_STATUSES = (
    "pending",
    "confirmed",
    "completed",
    "no_show",
    "cancelled_by_client",
    "cancelled_by_salon",
)
ACTIVE_BOOKING_STATUSES = ("pending", "confirmed", "completed", "no_show")

OVERLAP_CONSTRAINT = "ex_bookings_no_overlapping"


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def occupied_range_of(start_at: datetime, end_at: datetime, buffer_min: int) -> Range[datetime]:
    """The time a booking keeps the master: the service and the buffer after it."""
    return Range(start_at, end_at + timedelta(minutes=buffer_min), bounds="[)")


class Booking(Base):
    """One booking, with a snapshot of the service as it was booked.

    ``occupied_range`` is an ordinary column: PostgreSQL does not accept it as a
    generated one, because ``timestamptz + interval`` is not immutable. The
    constructor fills it, and a ``CHECK`` refuses a row where it disagrees with
    ``start_at``, ``end_at`` and ``buffer_min``.
    """

    __tablename__ = "bookings"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    # References to catalog and auth: plain UUIDs, never foreign keys.
    salon_id: Mapped[UUID]
    master_id: Mapped[UUID]
    client_user_id: Mapped[UUID]
    service_id: Mapped[UUID]

    # The snapshot. A later change of the price list does not rewrite history.
    service_name: Mapped[str] = mapped_column(Text)
    price: Mapped[Decimal]
    currency: Mapped[str] = mapped_column(String(3))
    duration_min: Mapped[int]
    buffer_min: Mapped[int] = mapped_column(server_default=text("0"))
    # The salon's policy as it stood when this was booked: cancelling needs
    # nothing from catalog, and the client cancels on the terms they booked on.
    cancel_deadline_min: Mapped[int] = mapped_column(server_default=text("240"))

    start_at: Mapped[datetime]
    end_at: Mapped[datetime]
    occupied_range: Mapped[Range[datetime]] = mapped_column(TSTZRANGE)

    status: Mapped[str] = mapped_column(String(32))
    created_by: Mapped[UUID]
    cancelled_by: Mapped[UUID | None] = mapped_column(default=None)
    cancel_reason: Mapped[str | None] = mapped_column(Text, default=None)
    cancelled_at: Mapped[datetime | None] = mapped_column(default=None)

    reminder_at: Mapped[datetime | None] = mapped_column(default=None)
    reminder_sent_at: Mapped[datetime | None] = mapped_column(default=None)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("duration_min > 0", name="duration_min_positive"),
        CheckConstraint("buffer_min >= 0", name="buffer_min_not_negative"),
        CheckConstraint("cancel_deadline_min >= 0", name="cancel_deadline_min_not_negative"),
        CheckConstraint("end_at > start_at", name="end_at_after_start_at"),
        CheckConstraint(
            "occupied_range = tstzrange(start_at, "
            "end_at + make_interval(mins => buffer_min), '[)')",
            name="occupied_range_matches_times",
        ),
        CheckConstraint(f"status IN ({_sql_list(BOOKING_STATUSES)})", name="status_known"),
        ExcludeConstraint(
            ("master_id", "="),
            ("occupied_range", "&&"),
            name=OVERLAP_CONSTRAINT,
            using="gist",
            where=text(f"status IN ({_sql_list(ACTIVE_BOOKING_STATUSES)})"),
        ),
        Index("ix_bookings_master_id_start_at", "master_id", "start_at"),
        Index("ix_bookings_client_user_id_start_at", "client_user_id", text("start_at DESC")),
        Index("ix_bookings_salon_id_start_at", "salon_id", "start_at"),
        # Only the candidates the reminder scheduler reads every minute.
        Index(
            "ix_bookings_reminder_at",
            "reminder_at",
            postgresql_where=text(
                "reminder_sent_at IS NULL AND status IN ('pending', 'confirmed')"
            ),
        ),
    )

    def __init__(
        self,
        *,
        salon_id: UUID,
        master_id: UUID,
        client_user_id: UUID,
        service_id: UUID,
        service_name: str,
        price: Decimal,
        currency: str,
        duration_min: int,
        start_at: datetime,
        end_at: datetime,
        status: str,
        created_by: UUID,
        buffer_min: int = 0,
        cancel_deadline_min: int = 240,
        reminder_at: datetime | None = None,
        id: UUID | None = None,
    ) -> None:
        super().__init__(
            id=id or uuid4(),
            salon_id=salon_id,
            master_id=master_id,
            client_user_id=client_user_id,
            service_id=service_id,
            service_name=service_name,
            price=price,
            currency=currency,
            duration_min=duration_min,
            buffer_min=buffer_min,
            cancel_deadline_min=cancel_deadline_min,
            start_at=start_at,
            end_at=end_at,
            occupied_range=occupied_range_of(start_at, end_at, buffer_min),
            status=status,
            created_by=created_by,
            reminder_at=reminder_at,
        )
