"""Publishing events to Kafka.

Two things travel next to the payload and both matter.

The **key** is the id of the aggregate. Without it the partitioner spreads the
events of one booking across partitions, ordering is only guaranteed inside a
partition, and ``booking.cancelled`` overtakes ``booking.created``.

The **headers** carry ``event_type``, ``correlation_id`` and ``traceparent``.
The first lets a consumer route without parsing the body, the second keeps one
identifier from the gateway to the notification, and the third keeps the trace
whole across the broker.

Only the outbox relay is supposed to call this. Publishing straight from a
scenario loses the atomicity between the state change and the event
(ADR-0004).
"""

from __future__ import annotations

from types import TracebackType
from typing import Self
from uuid import UUID

from aiokafka import AIOKafkaProducer
from opentelemetry import trace

from barber_common.events.envelope import EventEnvelope
from barber_common.logging import get_logger
from barber_common.tracing import inject_trace_context

__all__ = ["EventProducer", "MessageHeaders"]

MessageHeaders = list[tuple[str, bytes]]

_logger = get_logger(__name__)
_tracer = trace.get_tracer(__name__)


class EventProducer:
    """Producer of one service.

    ``producer_factory`` exists so a test can hand in a fake; production always
    uses aiokafka.
    """

    def __init__(
        self,
        *,
        bootstrap_servers: str,
        service_name: str,
        service_version: str = "0.1.0",
        client: AIOKafkaProducer | None = None,
    ) -> None:
        self.producer_name = f"{service_name}@{service_version}"
        self._client = client or AIOKafkaProducer(
            bootstrap_servers=bootstrap_servers,
            # Nothing weaker is acceptable: an event lost between the database
            # and the broker is a notification that never arrives.
            acks="all",
            enable_idempotence=True,
            linger_ms=5,
        )
        self._is_started = False

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.stop()

    async def start(self) -> None:
        if self._is_started:
            return
        await self._client.start()
        self._is_started = True

    async def stop(self) -> None:
        if not self._is_started:
            return
        await self._client.stop()
        self._is_started = False

    async def publish[PayloadT](
        self,
        *,
        topic: str,
        aggregate_id: UUID | str,
        envelope: EventEnvelope[PayloadT],
    ) -> None:
        """Send one event and wait for the brokers to acknowledge it.

        Waiting is the point: the relay marks the row as published only after
        this returns, so a broker that is down leaves the row for the next pass.
        """
        # The span is what puts a traceparent in the headers: without one the
        # W3C propagator has nothing to write, and the trace would restart on
        # the consumer instead of continuing through the broker.
        with _tracer.start_as_current_span(
            f"publish {topic}",
            kind=trace.SpanKind.PRODUCER,
            attributes={
                "messaging.system": "kafka",
                "messaging.destination.name": topic,
                "messaging.message.id": str(envelope.event_id),
            },
        ):
            await self._client.send_and_wait(
                topic,
                key=str(aggregate_id).encode("utf-8"),
                value=envelope.model_dump_json().encode("utf-8"),
                headers=_build_headers(envelope),
            )

        _logger.info(
            "event published",
            topic=topic,
            event_type=envelope.event_type,
            event_id=str(envelope.event_id),
        )


def _build_headers[PayloadT](envelope: EventEnvelope[PayloadT]) -> MessageHeaders:
    """Duplicate the routing fields of the envelope into message headers."""
    carrier: dict[str, str] = {}
    inject_trace_context(carrier)

    headers: MessageHeaders = [
        ("event_type", envelope.event_type.encode("utf-8")),
        ("event_id", str(envelope.event_id).encode("utf-8")),
    ]
    if envelope.correlation_id is not None:
        headers.append(("correlation_id", envelope.correlation_id.encode("utf-8")))
    headers.extend((name, value.encode("utf-8")) for name, value in sorted(carrier.items()))
    return headers
