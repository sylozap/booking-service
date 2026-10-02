"""Reading and writing the outbox.

The repository never commits. A scenario writes the booking and the event in
one transaction and commits once; a repository that committed on its own would
make that impossible.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from uuid import UUID, uuid4

from opentelemetry import trace
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from barber_common.context import get_causation_id, get_correlation_id
from barber_common.outbox.models import OutboxMessage
from barber_common.tracing import current_traceparent

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

        ``causation_id`` defaults to the event being handled, when this runs
        in a consumer.
        """
        event_id = uuid4()
        with _create_span(topic, event_type, count=1, event_id=event_id):
            message = OutboxMessage(
                id=event_id,
                topic=topic,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                event_type=event_type,
                event_version=event_version,
                payload=payload.model_dump(mode="json"),
                correlation_id=get_correlation_id(),
                causation_id=causation_id or get_causation_id(),
                traceparent=current_traceparent(),
            )
        self._session.add(message)
        await self._session.flush()
        return message

    async def add_batch(
        self,
        *,
        topic: str,
        aggregate_type: str,
        event_type: str,
        events: Sequence[tuple[UUID, BaseModel]],
        event_version: int = 1,
        causation_id: UUID | None = None,
    ) -> list[OutboxMessage]:
        """Queue many events of one type in one statement.

        For a single change that touches many aggregates -- a cascade -- where
        a flush per event would grow the transaction by a round trip for each.
        ``events`` pairs the id of each aggregate with its payload.
        """
        correlation_id = get_correlation_id()
        # One span for the batch: the events share their cause and their trace.
        with _create_span(topic, event_type, count=len(events)):
            traceparent = current_traceparent()
        messages = [
            OutboxMessage(
                topic=topic,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                event_type=event_type,
                event_version=event_version,
                payload=payload.model_dump(mode="json"),
                correlation_id=correlation_id,
                causation_id=causation_id or get_causation_id(),
                traceparent=traceparent,
            )
            for aggregate_id, payload in events
        ]
        self._session.add_all(messages)
        await self._session.flush()
        return messages

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


def _create_span(
    topic: str, event_type: str, *, count: int, event_id: UUID | None = None
) -> AbstractContextManager[trace.Span]:
    """The point in the trace where an event came to be.

    Its context is what the row stores and the consumer continues: the event
    is created here, in the transaction of the change, and only sent later.
    """
    attributes: dict[str, str | int] = {
        "messaging.system": "kafka",
        "messaging.operation.type": "create",
        "messaging.destination.name": topic,
        "messaging.batch.message_count": count,
        "barber.event_type": event_type,
    }
    if event_id is not None:
        attributes["messaging.message.id"] = str(event_id)
    return trace.get_tracer(__name__).start_as_current_span(
        f"create {topic}", kind=trace.SpanKind.PRODUCER, attributes=attributes
    )
