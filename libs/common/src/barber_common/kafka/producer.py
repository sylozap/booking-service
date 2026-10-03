"""Kafka producer for event envelopes.

The message key is the aggregate id, so the events of one aggregate stay in
order. Headers carry ``event_type``, ``correlation_id`` and ``traceparent``.
Only the outbox relay publishes through it.
"""

from __future__ import annotations

from types import TracebackType
from typing import Self
from uuid import UUID

from aiokafka import AIOKafkaProducer
from opentelemetry import trace
from opentelemetry.context import Context

from barber_common.events.envelope import EventEnvelope
from barber_common.logging import get_logger
from barber_common.tracing import TRACEPARENT_HEADER, inject_trace_context, links_to

__all__ = ["EventProducer", "MessageHeaders"]

MessageHeaders = list[tuple[str, bytes]]

_logger = get_logger(__name__)


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
        traceparent: str | None = None,
    ) -> None:
        """Send one event and wait for the brokers to acknowledge it.

        Waiting is the point: the relay marks the row as published only after
        this returns, so a broker that is down leaves the row for the next pass.

        ``traceparent`` is the context the event was written in, as the outbox
        stored it. The message carries that context rather than the one of the
        publish, so the consumer continues the trace of the write. The publish
        span is then a root linked to it, not its child: it happens later, in
        a pass of the relay that serves many requests and belongs to none.
        """
        # An empty context makes a root; None means "the current span".
        root = None if traceparent is None else Context()
        with trace.get_tracer(__name__).start_as_current_span(
            f"publish {topic}",
            context=root,
            kind=trace.SpanKind.PRODUCER,
            links=links_to(traceparent),
            attributes={
                "messaging.system": "kafka",
                "messaging.operation.type": "send",
                "messaging.destination.name": topic,
                "messaging.message.id": str(envelope.event_id),
            },
        ):
            await self._client.send_and_wait(
                topic,
                key=str(aggregate_id).encode("utf-8"),
                value=envelope.model_dump_json().encode("utf-8"),
                headers=_build_headers(envelope, traceparent=traceparent),
            )

        _logger.info(
            "event published",
            topic=topic,
            event_type=envelope.event_type,
            event_id=str(envelope.event_id),
        )


def _build_headers[PayloadT](
    envelope: EventEnvelope[PayloadT], *, traceparent: str | None
) -> MessageHeaders:
    """Duplicate the routing fields of the envelope into message headers.

    Without a stored context the current span goes in -- the publish span --
    so the consumer still continues a trace instead of starting one.
    """
    carrier: dict[str, str] = {}
    if traceparent is None:
        inject_trace_context(carrier)
    else:
        carrier[TRACEPARENT_HEADER] = traceparent

    headers: MessageHeaders = [
        ("event_type", envelope.event_type.encode("utf-8")),
        ("event_id", str(envelope.event_id).encode("utf-8")),
    ]
    if envelope.correlation_id is not None:
        headers.append(("correlation_id", envelope.correlation_id.encode("utf-8")))
    headers.extend((name, value.encode("utf-8")) for name, value in sorted(carrier.items()))
    return headers
