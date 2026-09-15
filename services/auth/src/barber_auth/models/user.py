"""The user of the platform: contacts, password hash, activity."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from barber_common.db.base import Base

__all__ = ["User"]


class User(Base):
    """One account.

    ``email`` is stored in lower case and ``phone`` in E.164, so the unique
    indexes compare normalised values.
    """

    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    email: Mapped[str] = mapped_column(String(254))
    phone: Mapped[str] = mapped_column(String(16))
    # Self describing: algorithm, parameters, salt and digest in one string, so
    # a change of parameters does not invalidate the hashes already stored.
    password_hash: Mapped[str] = mapped_column(String(255))

    # Null until the address is confirmed. A timestamp rather than a flag: the
    # moment answers questions a boolean cannot.
    email_confirmed_at: Mapped[datetime | None] = mapped_column(default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        # Functional index, so addresses differing only in case are one account.
        Index("uq_users_lower_email", func.lower(email), unique=True),
        Index("uq_users_phone", "phone", unique=True),
    )
