"""Reading and writing the outbox.

The repository never commits. A scenario writes the booking and the event in
one transaction and commits once; a repository that committed on its own would
make that impossible.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from barber_common.context import get_correlation_id
from barber_common.outbox.models import OutboxMessage

__all__ = ["OutboxRepository"]


class OutboxRepository:
    """Access to the outbox of one service."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        *,
        topic: str,
        aggregate_type: str,
        aggregate_id: UUID,
        event_type: str,
        payload: BaseModel,
        event_version: int = 1,
        causation_id: UUID | None = None,
    ) -> OutboxMessage:
        """Queue an event inside the caller's transaction.

        The payload arrives as a pydantic model and is stored as JSON: the
        schema is checked here, at the only place that knows it, and not when
        a consumer three services away fails to parse it.
        """
        message = OutboxMessage(
            topic=topic,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            event_version=event_version,
            payload=payload.model_dump(mode="json"),
            correlation_id=get_correlation_id(),
            causation_id=causation_id,
        )
        self._session.add(message)
        await self._session.flush()
        return message

    async def claim_batch(self, *, limit: int) -> Sequence[OutboxMessage]:
        """Take a batch of unpublished rows and hold them for this transaction.

        ``FOR UPDATE SKIP LOCKED`` is what makes two relay replicas safe: the
        second one steps over the rows the first is holding instead of waiting
        for them or, worse, publishing them a second time.
        """
        statement = (
            select(OutboxMessage)
            .where(OutboxMessage.published_at.is_(None))
            .order_by(OutboxMessage.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await self._session.execute(statement)
        return result.scalars().all()

    async def mark_published(self, ids: Sequence[UUID]) -> None:
        """Mark the rows as published, in the transaction that holds them."""
        if not ids:
            return
        await self._session.execute(
            update(OutboxMessage)
            .where(OutboxMessage.id.in_(ids))
            .values(published_at=datetime.now(UTC))
        )

    async def count_pending(self) -> int:
        """Number of events waiting. Growing means the relay is stuck."""
        result = await self._session.execute(
            select(func.count())
            .select_from(OutboxMessage)
            .where(OutboxMessage.published_at.is_(None))
        )
        return int(result.scalar_one())

    async def oldest_pending_age_seconds(self) -> float:
        """Age of the oldest unpublished event, zero when there is none.

        This is the number that says how far the database and the broker have
        drifted apart, and the one an alert watches.
        """
        result = await self._session.execute(
            select(func.min(OutboxMessage.created_at)).where(OutboxMessage.published_at.is_(None))
        )
        oldest = result.scalar_one_or_none()
        if oldest is None:
            return 0.0
        return max(0.0, (datetime.now(UTC) - oldest).total_seconds())
