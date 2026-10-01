"""The journal of notifications, which is also the queue of deliveries."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from barber_common.db.base import Base

__all__ = ["CHANNELS", "NOTIFICATION_STATUSES", "Notification"]

CHANNELS = ("email", "telegram")
NOTIFICATION_STATUSES = ("pending", "sending", "sent", "failed")


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class Notification(Base):
    """One message to one user on one channel.

    Written ``pending`` by a consumer, in the transaction that marks the event
    processed; sent later by the delivery worker, outside any transaction.
    ``dedup_key`` is ``{event_id}:{channel}``, and its uniqueness is what keeps
    an event delivered twice from becoming two messages.

    ``payload`` holds the fields the template is rendered with, not the text:
    the address and the text are resolved when the message is sent. The one
    exception is ``address``, set when the event names it -- a confirmation
    letter goes to the address being confirmed. ``topic`` is where the event
    came from: a notification given up on goes to the dead letter topic of it.
    ``next_attempt_at`` is when the worker may take the row: now for a new one,
    later for a retry, and the end of the lease for one being sent.
    ``traceparent`` and ``correlation_id`` are those of the event being handled
    when the row was queued: the send happens later, in the worker, and
    continues that trace and that correlation instead of starting its own.
    """

    __tablename__ = "notifications"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID]
    event_id: Mapped[UUID]
    topic: Mapped[str] = mapped_column(String(255))
    channel: Mapped[str] = mapped_column(String(16))
    template: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    dedup_key: Mapped[str] = mapped_column(String(128), unique=True)
    address: Mapped[str | None] = mapped_column(String(320), default=None)
    traceparent: Mapped[str | None] = mapped_column(String(128), default=None)
    correlation_id: Mapped[str | None] = mapped_column(String(64), default=None)

    status: Mapped[str] = mapped_column(String(16), server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    next_attempt_at: Mapped[datetime] = mapped_column(server_default=func.now())

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(default=None)

    __table_args__ = (
        CheckConstraint(f"channel IN ({_sql_list(CHANNELS)})", name="channel_known"),
        CheckConstraint(f"status IN ({_sql_list(NOTIFICATION_STATUSES)})", name="status_known"),
        CheckConstraint("attempts >= 0", name="attempts_not_negative"),
        # What the delivery worker scans every pass: only rows it may take.
        Index(
            "ix_notifications_next_attempt_at",
            "next_attempt_at",
            postgresql_where=text("status IN ('pending', 'sending')"),
        ),
        # A user's history, newest first.
        Index("ix_notifications_user_id_created_at", "user_id", text("created_at DESC")),
    )
