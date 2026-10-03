"""The send continues the trace and the correlation of the event behind it.

A notification is queued by a consumer and sent later by the worker, outside
that consumer. The row carries the context across, and these tests follow it:
from the span the row was queued in to the span of the send, and on to the
dead letter written when the worker gives up.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from typing import Protocol
from uuid import UUID, uuid4

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from barber_common.context import bind_context
from barber_common.events.bookings import BOOKINGS_TOPIC
from barber_common.kafka.dlq import dlq_topic_of
from barber_common.outbox.models import OutboxMessage
from barber_common.testing import recorded_spans
from barber_common.tracing import context_of
from barber_notification.models.notification import Notification
from barber_notification.models.recipient import Recipient
from barber_notification.preferences import NotificationKind
from barber_notification.providers.base import DeliveryResult
from barber_notification.services.dispatch import EnqueueNotification
from barber_notification.workers.delivery import DeliveryWorker

pytestmark = pytest.mark.integration

RecipientFactory = Callable[..., Awaitable[Recipient]]
CORRELATION_ID = "c-trace-1"
FIELDS: dict[str, object] = {
    "service_name": "Стрижка",
    "start_at": "05.09.2026 15:00 (Europe/Moscow)",
}


class RecordingProvider(Protocol):
    """The provider of conftest."""

    answers: list[DeliveryResult]
    correlation_ids: list[str | None]


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    with recorded_spans() as exporter:
        yield exporter


def tracer() -> trace.Tracer:
    """Taken per call: a tracer held by the module keeps the provider of the first test."""
    return trace.get_tracer(__name__)


async def queue_in_consumer(session: AsyncSession, user_id: UUID) -> trace.SpanContext:
    """Queue a message the way the consumer does: inside its span, under its correlation id."""
    with (
        bind_context(correlation_id=CORRELATION_ID),
        tracer().start_as_current_span("consume booking.bookings.v1") as consume,
    ):
        await EnqueueNotification(session).execute(
            event_id=uuid4(),
            topic=BOOKINGS_TOPIC,
            user_id=user_id,
            kind=NotificationKind.BOOKINGS,
            template="booking_created",
            fields=FIELDS,
        )
    return consume.get_span_context()


def send_spans(exporter: InMemorySpanExporter) -> list[ReadableSpan]:
    return [span for span in exporter.get_finished_spans() if span.name.startswith("send ")]


async def test_a_queued_row_keeps_the_trace_and_the_correlation_of_the_event(
    session: AsyncSession, make_recipient: RecipientFactory, spans: InMemorySpanExporter
) -> None:
    client = await make_recipient(email="client@example.com")

    consume = await queue_in_consumer(session, client.user_id)
    row = (
        await session.execute(select(Notification).where(Notification.user_id == client.user_id))
    ).scalar_one()
    stored = trace.get_current_span(context_of(row.traceparent)).get_span_context()

    assert (stored.trace_id, stored.span_id) == (consume.trace_id, consume.span_id)
    assert row.correlation_id == CORRELATION_ID


async def test_the_send_continues_the_trace_of_the_event(
    worker: DeliveryWorker,
    session: AsyncSession,
    make_recipient: RecipientFactory,
    spans: InMemorySpanExporter,
) -> None:
    client = await make_recipient(email="client@example.com")
    consume = await queue_in_consumer(session, client.user_id)

    await worker.run_once()
    [send] = send_spans(spans)

    assert send.name == "send email"
    assert send.parent is not None
    assert (send.parent.trace_id, send.parent.span_id) == (consume.trace_id, consume.span_id)


async def test_the_send_runs_under_the_correlation_id_of_the_event(
    worker: DeliveryWorker,
    email: RecordingProvider,
    session: AsyncSession,
    make_recipient: RecipientFactory,
) -> None:
    client = await make_recipient(email="client@example.com")
    await queue_in_consumer(session, client.user_id)

    await worker.run_once()

    assert email.correlation_ids == [CORRELATION_ID]


async def test_a_failed_attempt_is_an_error_of_its_span(
    worker: DeliveryWorker,
    email: RecordingProvider,
    session: AsyncSession,
    make_recipient: RecipientFactory,
    spans: InMemorySpanExporter,
) -> None:
    client = await make_recipient(email="client@example.com")
    await queue_in_consumer(session, client.user_id)
    email.answers.append(DeliveryResult.temporary("smtp: 451"))

    await worker.run_once()
    [send] = send_spans(spans)

    assert send.status.status_code is trace.StatusCode.ERROR
    assert send.attributes is not None
    assert send.attributes["barber.notification.outcome"] == "temporary_failure"


async def test_the_dead_letter_of_a_message_given_up_on_stays_in_the_trace(
    worker: DeliveryWorker,
    email: RecordingProvider,
    session: AsyncSession,
    make_recipient: RecipientFactory,
    spans: InMemorySpanExporter,
) -> None:
    client = await make_recipient(email="client@example.com")
    consume = await queue_in_consumer(session, client.user_id)
    email.answers.append(DeliveryResult.permanent("smtp: 550 no such user"))

    await worker.run_once()
    record = (
        await session.execute(
            select(OutboxMessage).where(OutboxMessage.topic == dlq_topic_of(BOOKINGS_TOPIC))
        )
    ).scalar_one()

    stored = trace.get_current_span(context_of(record.traceparent)).get_span_context()
    assert stored.trace_id == consume.trace_id
