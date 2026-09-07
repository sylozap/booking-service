"""Machine to machine callers: one row per service."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ARRAY, Boolean, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from barber_common.db.base import Base

__all__ = ["ServiceClient"]


class ServiceClient(Base):
    """Credentials a service presents to get a token of its own.

    The secret is stored as a hash, like a password: an internal caller is
    still a caller, and a leaked table must not be a set of working
    credentials.
    """

    __tablename__ = "service_clients"

    client_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    secret_hash: Mapped[str] = mapped_column(String(255))
    # What the token this client receives is allowed to do. An array rather
    # than a joined table: the list is short, fixed at deploy time and always
    # read whole.
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
