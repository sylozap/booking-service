"""Public halves of the keys the access tokens are signed with."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Index, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from barber_common.db.base import Base

__all__ = ["SigningKey"]


class SigningKey(Base):
    """The public key of one signing key pair, by the ``kid`` of the JWT header.

    A rotation adds a second active key instead of replacing the first.
    """

    __tablename__ = "signing_keys"

    kid: Mapped[str] = mapped_column(String(64), primary_key=True)

    public_pem: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        # What JWKS serves, and the only query this table has.
        Index("ix_signing_keys_is_active", "is_active", postgresql_where=text("is_active")),
    )
