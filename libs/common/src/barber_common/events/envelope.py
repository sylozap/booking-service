"""The envelope every event travels in.

Fixed by docs/07-events-and-kafka.md. Changing a field of the envelope breaks
every consumer of every topic at once, so it is not changed: a payload grows,
the envelope does not.

The envelope is generic in its payload. ``EventEnvelope[BookingCreated]`` is
what a producer builds and what a consumer parses, and mypy compares the two
sides at build time -- the whole reason there is no Schema Registry here
(ADR-0005).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict

from barber_common.context import get_correlation_id

__all__ = ["ENVELOPE_VERSION", "EventEnvelope", "JsonEnvelope", "build_envelope"]

ENVELOPE_VERSION = 1


class EventEnvelope[PayloadT](BaseModel):
    """Metadata carried by every message on every topic.

    ``extra="ignore"`` makes every consumer a tolerant reader: a field added by
    a newer producer is skipped instead of stopping a partition.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    # Also the deduplication key: a consumer records it in processed_events and
    # a redelivery has no second effect.
    event_id: UUID
    event_type: str
    event_version: int = ENVELOPE_VERSION
    occurred_at: AwareDatetime
    correlation_id: str | None = None
    # event_id of the event that caused this one, when there is one.
    causation_id: UUID | None = None
    producer: str
    payload: PayloadT


# What a relay and a dead letter handler read: the payload stays as it was sent.
JsonEnvelope = EventEnvelope[dict[str, object]]


def build_envelope[PayloadT](
    *,
    event_type: str,
    payload: PayloadT,
    producer: str,
    event_id: UUID | None = None,
    event_version: int = ENVELOPE_VERSION,
    occurred_at: datetime | None = None,
    correlation_id: str | None = None,
    causation_id: UUID | None = None,
) -> EventEnvelope[PayloadT]:
    """Build an envelope, filling in what the context already knows.

    ``event_id`` is accepted rather than always generated: the outbox uses the
    id of its row, so that republishing after a crash carries the same id and
    the consumer recognises the duplicate.
    """
    return EventEnvelope(
        event_id=event_id or uuid4(),
        event_type=event_type,
        event_version=event_version,
        occurred_at=occurred_at or datetime.now(UTC),
        correlation_id=correlation_id or get_correlation_id(),
        causation_id=causation_id,
        producer=producer,
        payload=payload,
    )
