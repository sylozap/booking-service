"""One line of a weekly schedule: "Monday 10:00-20:00, from this date on"."""

from __future__ import annotations

from datetime import date, datetime, time
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, ForeignKey, Index, SmallInteger, func
from sqlalchemy.orm import Mapped, mapped_column

from barber_common.db.base import Base

__all__ = ["ScheduleTemplate"]


class ScheduleTemplate(Base):
    """A working interval on one weekday, in the salon's local time.

    ``weekday`` counts from Monday as 0. A day may have several intervals;
    that they do not overlap is checked by the domain before writing.
    ``valid_to`` is inclusive and open-ended when null.
    """

    __tablename__ = "schedule_templates"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    master_id: Mapped[UUID] = mapped_column(
        ForeignKey("master_settings.master_id", ondelete="CASCADE")
    )

    weekday: Mapped[int] = mapped_column(SmallInteger)
    start_time: Mapped[time]
    end_time: Mapped[time]

    valid_from: Mapped[date]
    valid_to: Mapped[date | None] = mapped_column(default=None)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint("weekday BETWEEN 0 AND 6", name="weekday_in_week"),
        CheckConstraint("end_time > start_time", name="end_time_after_start_time"),
        CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from", name="valid_to_not_before_valid_from"
        ),
        Index("ix_schedule_templates_master_id_weekday", "master_id", "weekday"),
    )
