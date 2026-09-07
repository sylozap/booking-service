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

    ``email`` and ``phone`` are stored normalised -- lower case and E.164 --
    because the uniqueness of a user is decided by the indexes below, and an
    index compares what it was given. Normalising on read would leave the
    database holding four spellings of one phone number.
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
        # Functional: docs/05-data-model.md fixes uniqueness on lower(email),
        # and a plain unique constraint would let Ivan@mail and ivan@mail be
        # two accounts of one person.
        Index("uq_users_lower_email", func.lower(email), unique=True),
        Index("uq_users_phone", "phone", unique=True),
    )
