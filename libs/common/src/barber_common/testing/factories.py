"""Factories for the objects the chassis itself defines.

A factory, not a ready-made object: the test names the two or three fields its
assertion is about, and everything else gets a valid default. A fixture that
hands out a finished object forces every test to know fields it does not care
about, and the day one of them gains a constraint, every test changes.

Domain factories -- bookings, masters, salons -- belong to the service that
owns them. Only what lives in ``barber_common`` is here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from barber_common.events.envelope import JsonEnvelope, build_envelope
from barber_common.kafka.dlq import DeadLetter
from barber_common.outbox.models import OutboxMessage

__all__ = ["dead_letter_factory", "envelope_factory", "outbox_message_factory"]

DEFAULT_TOPIC = "test.aggregates.v1"


def envelope_factory(
    *,
    event_type: str = "test.happened",
    payload: dict[str, object] | None = None,
    event_id: UUID | None = None,
    producer: str = "test@0.1.0",
    correlation_id: str | None = None,
    causation_id: UUID | None = None,
    occurred_at: datetime | None = None,
) -> JsonEnvelope:
    """Build an envelope with a payload that is already JSON."""
    return build_envelope(
        event_type=event_type,
        payload=payload if payload is not None else {"value": "test"},
        producer=producer,
        event_id=event_id,
        correlation_id=correlation_id,
        causation_id=causation_id,
        occurred_at=occurred_at or datetime.now(UTC),
    )


def outbox_message_factory(
    *,
    topic: str = DEFAULT_TOPIC,
    aggregate_type: str = "aggregates",
    aggregate_id: UUID | None = None,
    event_type: str = "test.happened",
    payload: dict[str, object] | None = None,
    correlation_id: str | None = None,
    published_at: datetime | None = None,
) -> OutboxMessage:
    """Build an unsaved outbox row.

    Unsaved on purpose: the test decides which transaction it belongs to, and
    that decision is what most outbox tests are about.
    """
    return OutboxMessage(
        id=uuid4(),
        topic=topic,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id or uuid4(),
        event_type=event_type,
        event_version=1,
        payload=payload if payload is not None else {"value": "test"},
        correlation_id=correlation_id,
        published_at=published_at,
    )


def dead_letter_factory(
    *,
    original_topic: str = DEFAULT_TOPIC,
    reason: str = "handler_failed",
    detail: str = "RuntimeError: test",
    attempts: int = 3,
    value: bytes = b'{"event_type": "test.happened"}',
    key: bytes | None = None,
    traceback: str | None = None,
) -> DeadLetter:
    """Build a dead letter as a consumer would hand it to the publisher."""
    return DeadLetter(
        original_topic=original_topic,
        reason=reason,
        detail=detail,
        attempts=attempts,
        value=value,
        key=key,
        traceback=traceback,
    )
