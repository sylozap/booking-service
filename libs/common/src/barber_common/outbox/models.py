"""The outbox table, identical in every service that publishes events.

A row is written in the same transaction as the state change it describes.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from barber_common.db.base import Base

__all__ = ["OutboxMessage"]


class OutboxMessage(Base):
    """One event waiting to be published.

    ``id`` doubles as the ``event_id`` of the envelope, so a republished event
    is recognised by consumers as a duplicate.
    """

    __tablename__ = "outbox"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    # Stored rather than derived from the aggregate: the version lives in the
    # topic name, and deriving it would hardcode "v1" in the relay.
    topic: Mapped[str] = mapped_column(String(255))
    aggregate_type: Mapped[str] = mapped_column(String(64))
    # The partitioning key. No foreign key: it may point at another service.
    aggregate_id: Mapped[UUID]

    event_type: Mapped[str] = mapped_column(String(128))
    event_version: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)

    correlation_id: Mapped[str | None] = mapped_column(String(64), default=None)
    causation_id: Mapped[UUID | None] = mapped_column(default=None)
    # The trace the event was written in, picked up again by the relay. Empty
    # while tracing is off.
    traceparent: Mapped[str | None] = mapped_column(String(128), default=None)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(default=None)

    __table_args__ = (
        # Partial: the table keeps published rows for a while, and the relay
        # only ever looks at the unpublished ones. A full index would grow with
        # the history and slow the hot path down for no reason.
        Index(
            "ix_outbox_created_at",
            "created_at",
            postgresql_where=text("published_at IS NULL"),
        ),
    )
