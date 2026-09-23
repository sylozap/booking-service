"""What booking keeps about a master: the buffer, the zone and who they are."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Boolean, CheckConstraint, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column, validates

from barber_common.db.base import Base

__all__ = ["MasterSettings"]


class MasterSettings(Base):
    """The aggregate a schedule and the bookings of one master hang off.

    Created from ``master.created``; not a replica of the profile in
    ``catalog``. ``user_id`` is the account behind the profile, kept so a master
    can be recognised as the owner of this schedule. ``timezone`` is the salon's
    IANA zone, in which the schedule is written.
    """

    __tablename__ = "master_settings"

    master_id: Mapped[UUID] = mapped_column(primary_key=True)
    salon_id: Mapped[UUID]
    user_id: Mapped[UUID]

    timezone: Mapped[str] = mapped_column(String(64))
    buffer_after_min: Mapped[int] = mapped_column(server_default=text("0"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("buffer_after_min >= 0", name="buffer_after_min_not_negative"),
        # The masters of one salon, for the administrator's views.
        Index("ix_master_settings_salon_id", "salon_id"),
    )

    @validates("timezone")
    def _validate_timezone(self, _key: str, value: str) -> str:
        """Refuse a zone that cannot be resolved, on every write path."""
        try:
            ZoneInfo(value)
        except (ValueError, KeyError) as error:
            raise ValueError(f"{value!r} is not a known IANA time zone") from error
        return value
