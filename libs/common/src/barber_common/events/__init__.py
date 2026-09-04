"""Schemas of the asynchronous messages of the platform.

One source of truth for the producer and the consumer: a payload that stops
matching is caught by mypy on both sides rather than at three in the morning
in a consumer log.
"""

from barber_common.events.envelope import (
    ENVELOPE_VERSION,
    EventEnvelope,
    JsonEnvelope,
    build_envelope,
)

__all__ = ["ENVELOPE_VERSION", "EventEnvelope", "JsonEnvelope", "build_envelope"]
