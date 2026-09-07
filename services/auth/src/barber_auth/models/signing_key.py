"""Public halves of the keys the access tokens are signed with."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Index, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from barber_common.db.base import Base

__all__ = ["SigningKey"]


class SigningKey(Base):
    """One key pair, by the ``kid`` that appears in the JWT header.

    Only the public half is here; the private one is a Kubernetes Secret. A
    rotation adds a second active key rather than replacing the first, so the
    tokens signed a minute ago keep verifying while the new ones are issued
    (docs/04-api-contracts.md).
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
