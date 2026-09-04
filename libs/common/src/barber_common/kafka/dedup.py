"""Deduplication of incoming events.

Delivery is at-least-once, so the same event arrives more than once whenever a
consumer dies between the effect and the offset commit. The defence is a table:
``processed_events`` holds the pair ``(event_id, consumer_group)``, and the row
is written in the same transaction as the effect. Either both are there or
neither is, and a redelivery finds the row and stops.

The pair, not the id alone: two consumer groups read the same topic and both
have to see the event once (ADR-0007).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import String, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from barber_common.db.base import Base

__all__ = ["ProcessedEvent", "ProcessedEventRepository"]


class ProcessedEvent(Base):
    """One event already handled by one consumer group."""

    __tablename__ = "processed_events"

    event_id: Mapped[UUID] = mapped_column(primary_key=True)
    consumer_group: Mapped[str] = mapped_column(String(128), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ProcessedEventRepository:
    """The deduplication table of one consumer group."""

    def __init__(self, session: AsyncSession, *, consumer_group: str) -> None:
        self._session = session
        self._consumer_group = consumer_group

    async def claim(self, event_id: UUID) -> bool:
        """Take the event for this group, reporting whether it is new.

        ``INSERT ... ON CONFLICT DO NOTHING`` rather than "select, then insert":
        two replicas of one group can be handling the same redelivery at the
        same moment, and between the select and the insert both would decide
        the event is new. Here the second one blocks on the row of the first,
        then gets nothing back and knows it lost.

        Called inside the transaction that carries the effect: a commit that
        writes the effect writes this row, and a rollback takes both away.
        """
        statement = (
            insert(ProcessedEvent)
            .values(event_id=event_id, consumer_group=self._consumer_group)
            .on_conflict_do_nothing(index_elements=["event_id", "consumer_group"])
            .returning(ProcessedEvent.event_id)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def is_processed(self, event_id: UUID) -> bool:
        """Report whether this group has already handled the event."""
        result = await self._session.execute(
            select(ProcessedEvent.event_id).where(
                ProcessedEvent.event_id == event_id,
                ProcessedEvent.consumer_group == self._consumer_group,
            )
        )
        return result.scalar_one_or_none() is not None
