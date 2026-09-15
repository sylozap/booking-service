"""Deduplication of incoming events.

``processed_events`` stores ``(event_id, consumer_group)`` in the same
transaction as the effect of the handler, so a redelivered event is detected
and skipped. The consumer group is part of the key because several groups read
the same topic.
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

        Uses ``INSERT ... ON CONFLICT DO NOTHING``, so two replicas handling the
        same redelivery cannot both claim it. Called inside the transaction that
        carries the effect.
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
