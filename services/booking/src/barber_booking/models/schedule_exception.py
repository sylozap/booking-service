"""A change to the schedule of one date: a day off, other hours, a break."""

from __future__ import annotations

from datetime import date, datetime, time
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from barber_common.db.base import Base

__all__ = ["ScheduleException"]


class ScheduleException(Base):
    """One exception on one date, in the salon's local time.

    ``day_off`` carries no times; ``custom_hours`` and ``break`` carry both.
    Which kinds may share a date is a rule of the domain.
    """

    __tablename__ = "schedule_exceptions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    master_id: Mapped[UUID] = mapped_column(
        ForeignKey("master_settings.master_id", ondelete="CASCADE")
    )

    effective_on: Mapped[date]
    kind: Mapped[str] = mapped_column(String(16))
    start_time: Mapped[time | None] = mapped_column(default=None)
    end_time: Mapped[time | None] = mapped_column(default=None)
    reason: Mapped[str | None] = mapped_column(Text, default=None)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint("kind IN ('day_off', 'custom_hours', 'break')", name="kind_known"),
        CheckConstraint(
            "(kind = 'day_off' AND start_time IS NULL AND end_time IS NULL)"
            " OR (kind <> 'day_off' AND start_time IS NOT NULL AND end_time > start_time)",
            name="times_match_kind",
        ),
        Index("ix_schedule_exceptions_master_id_effective_on", "master_id", "effective_on"),
    )
