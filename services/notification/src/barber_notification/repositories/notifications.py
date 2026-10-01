"""The journal of notifications, and the queue the delivery worker reads."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from barber_notification.models.notification import Notification

__all__ = ["NotificationRecord", "NotificationRepository", "NotificationStatus", "dedup_key_of"]


class NotificationStatus(StrEnum):
    """Where one notification is. Also the column ``notifications.status``."""

    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"


# What the worker may take: new rows, retries whose time came, and rows whose
# lease ran out while being sent -- their worker is presumed dead.
_TAKEABLE = (NotificationStatus.PENDING.value, NotificationStatus.SENDING.value)


@dataclass(frozen=True, slots=True)
class NotificationRecord:
    """One notification as the delivery worker sees it."""

    id: UUID
    user_id: UUID
    event_id: UUID
    topic: str
    channel: str
    template: str
    payload: dict[str, object]
    address: str | None
    attempts: int
    traceparent: str | None = None
    correlation_id: str | None = None


def _to_record(row: Notification) -> NotificationRecord:
    return NotificationRecord(
        id=row.id,
        user_id=row.user_id,
        event_id=row.event_id,
        topic=row.topic,
        channel=row.channel,
        template=row.template,
        payload=dict(row.payload),
        address=row.address,
        attempts=row.attempts,
        traceparent=row.traceparent,
        correlation_id=row.correlation_id,
    )


def dedup_key_of(event_id: UUID, channel: str) -> str:
    """One notification per event and channel, whatever the delivery count."""
    return f"{event_id}:{channel}"


class NotificationRepository:
    """Access to ``notifications``. Never commits."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_pending(
        self,
        *,
        user_id: UUID,
        event_id: UUID,
        topic: str,
        channel: str,
        template: str,
        payload: dict[str, object],
        address: str | None = None,
        traceparent: str | None = None,
        correlation_id: str | None = None,
    ) -> bool:
        """Queue one notification. Whether it is new.

        ``ON CONFLICT DO NOTHING`` on the dedup key: a second delivery of the
        same event is a no-op here, not an error.

        ``traceparent`` and ``correlation_id`` are what the send continues.
        """
        statement = (
            insert(Notification)
            .values(
                user_id=user_id,
                event_id=event_id,
                topic=topic,
                channel=channel,
                template=template,
                payload=payload,
                address=address,
                traceparent=traceparent,
                correlation_id=correlation_id,
                dedup_key=dedup_key_of(event_id, channel),
            )
            .on_conflict_do_nothing(index_elements=[Notification.dedup_key])
            .returning(Notification.id)
        )
        return (await self._session.execute(statement)).scalar_one_or_none() is not None

    async def claim_due(
        self, *, now: datetime, limit: int, lease: timedelta
    ) -> list[NotificationRecord]:
        """Take up to ``limit`` notifications whose time has come.

        ``SKIP LOCKED`` so two workers take different rows. Each taken row is
        marked ``sending`` until ``now + lease`` and counts one more attempt;
        a worker that dies mid-send leaves it to be taken again after that.
        """
        rows = (
            (
                await self._session.execute(
                    select(Notification)
                    .where(
                        Notification.status.in_(_TAKEABLE),
                        Notification.next_attempt_at <= now,
                    )
                    .order_by(Notification.next_attempt_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return []

        await self._session.execute(
            update(Notification)
            .where(Notification.id.in_([row.id for row in rows]))
            .values(
                status=NotificationStatus.SENDING.value,
                attempts=Notification.attempts + 1,
                next_attempt_at=now + lease,
            )
            .execution_options(synchronize_session=False)
        )
        # The rows were read before the update; the attempt just started counts.
        return [replace(_to_record(row), attempts=row.attempts + 1) for row in rows]

    async def count_unsent(self) -> int:
        """Notifications still in the queue: waiting, being sent, or due for a retry."""
        statement = (
            select(func.count()).select_from(Notification).where(Notification.status.in_(_TAKEABLE))
        )
        return int((await self._session.execute(statement)).scalar_one())

    async def oldest_due_age_seconds(self, *, now: datetime) -> float:
        """How long the longest-waiting due notification has waited, zero if none is due.

        A row whose lease ran out counts from the end of the lease: from then on
        it is due again and waiting for a worker.
        """
        statement = select(func.min(Notification.next_attempt_at)).where(
            Notification.status.in_(_TAKEABLE), Notification.next_attempt_at <= now
        )
        oldest = (await self._session.execute(statement)).scalar_one_or_none()
        if oldest is None:
            return 0.0
        return max(0.0, (now - oldest).total_seconds())

    async def mark_sent(
        self, notification_id: UUID, *, now: datetime, forget_payload: bool
    ) -> None:
        """Record the delivery. ``forget_payload`` drops fields that held a credential."""
        values: dict[str, object] = {
            "status": NotificationStatus.SENT.value,
            "sent_at": now,
            "last_error": None,
        }
        if forget_payload:
            values["payload"] = {}
        await self._set(notification_id, values)

    async def mark_failed(self, notification_id: UUID, *, error: str) -> None:
        """Give up: the failure is permanent or the attempts ran out."""
        await self._set(
            notification_id,
            {"status": NotificationStatus.FAILED.value, "last_error": error},
        )

    async def retry_at(self, notification_id: UUID, *, at: datetime, error: str) -> None:
        """Put the notification back in the queue for a later attempt."""
        await self._set(
            notification_id,
            {
                "status": NotificationStatus.PENDING.value,
                "next_attempt_at": at,
                "last_error": error,
            },
        )

    async def _set(self, notification_id: UUID, values: dict[str, object]) -> None:
        await self._session.execute(
            update(Notification).where(Notification.id == notification_id).values(**values)
        )
