"""OpenTelemetry tracing across service boundaries.

Tracing is switched by ``OTLP_ENABLED``. While it is off, spans are
non-recording and the log fields ``trace_id`` and ``span_id`` stay ``null``.
Kafka is instrumented in the producer and the consumer, where the trace context
is written to and read from message headers.

An event crosses two asynchronous gaps on its way, the outbox and the broker,
and the trace is carried over both. Writing an event opens a ``create`` span
in the trace of whatever wrote it, and its ``traceparent`` is stored with the
row. The relay publishes later, outside that trace: its ``publish`` span starts
a trace of its own and links to the stored context instead of descending from
it, while the message headers carry the stored context on. The consumer
continues from there, so one trace runs from the request to the effect of the
event, with the relay beside it.

Modules take their tracer at the moment they start a span instead of holding
one: a tracer held from import keeps the first provider it resolved for the
life of the process, and a test suite installs a provider per test.
"""

from __future__ import annotations

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.semconv.resource import ResourceAttributes
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from sqlalchemy.ext.asyncio import AsyncEngine

from barber_common.config import BaseAppSettings
from barber_common.logging import get_logger

__all__ = [
    "TRACEPARENT_HEADER",
    "OrphanDatabaseSpanFilter",
    "configure_tracing",
    "context_of",
    "current_traceparent",
    "extract_trace_context",
    "inject_trace_context",
    "instrument_engine",
    "instrument_fastapi",
    "links_to",
]

TRACEPARENT_HEADER = "traceparent"

# Both spellings: the attribute was renamed by the database semantic
# conventions, and the instrumentation emits either, depending on opt-in.
_DATABASE_SYSTEM_ATTRIBUTES = ("db.system", "db.system.name")

_logger = get_logger(__name__)
_propagator = TraceContextTextMapPropagator()


def configure_tracing(
    settings: BaseAppSettings,
    *,
    span_exporter: SpanExporter | None = None,
) -> None:
    """Install the tracer provider of the process.

    ``span_exporter`` replaces the OTLP exporter; tests pass an in-memory one.
    When tracing is disabled and no exporter is given, nothing is installed and
    the no-op provider of the API stays in place.
    """
    if span_exporter is None and not settings.otlp_enabled:
        _logger.info("tracing is disabled", otlp_enabled=False)
        return

    exporter = span_exporter
    if exporter is None:
        if settings.otlp_endpoint is None:
            raise ValueError("OTLP_ENABLED is set but OTLP_ENDPOINT is empty")
        exporter = OTLPSpanExporter(endpoint=settings.otlp_endpoint)

    resource = Resource.create(
        {
            ResourceAttributes.SERVICE_NAME: settings.service_name,
            ResourceAttributes.DEPLOYMENT_ENVIRONMENT: settings.environment.value,
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(OrphanDatabaseSpanFilter(BatchSpanProcessor(exporter)))
    trace.set_tracer_provider(provider)

    # The outgoing side of every synchronous call between services.
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    _instrument_redis(provider)

    _logger.info("tracing is enabled", otlp_endpoint=settings.otlp_endpoint)


class OrphanDatabaseSpanFilter(SpanProcessor):
    """Drops database spans that belong to no operation.

    The relay and the delivery worker poll their tables every second, and
    every poll is a statement. Outside a request or a message there is no span
    to hang it on, so each one would become a trace of its own, and the search
    in Tempo would drown in lone ``SELECT ... FOR UPDATE``. A statement that
    is part of something keeps its span.

    A filter on export rather than a sampler: the instrumentation names the
    database only after the span has started, when the sampler has already
    decided.
    """

    def __init__(self, delegate: SpanProcessor) -> None:
        self._delegate = delegate

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        self._delegate.on_start(span, parent_context=parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        if span.parent is None and _is_database_span(span):
            return
        self._delegate.on_end(span)

    def shutdown(self) -> None:
        self._delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._delegate.force_flush(timeout_millis)


def _is_database_span(span: ReadableSpan) -> bool:
    attributes = span.attributes or {}
    return any(name in attributes for name in _DATABASE_SYSTEM_ATTRIBUTES)


def _instrument_redis(provider: TracerProvider) -> None:
    """Instrument Redis if the instrumentation package is installed."""
    try:
        from opentelemetry.instrumentation.redis import RedisInstrumentor
    except ImportError:
        return
    RedisInstrumentor().instrument(tracer_provider=provider)


def instrument_fastapi(app: FastAPI) -> None:
    """Create a server span for every request handled by this application."""
    FastAPIInstrumentor.instrument_app(
        app,
        excluded_urls="/health/live,/health/ready,/metrics",
    )


def instrument_engine(engine: AsyncEngine) -> None:
    """Create a span for every statement the service sends to its database."""
    SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)


def inject_trace_context(carrier: dict[str, str]) -> dict[str, str]:
    """Write the current span into a carrier, W3C style.

    Used for Kafka headers, so a trace continues on the other side of the
    broker instead of starting again from the consumer.
    """
    _propagator.inject(carrier)
    return carrier


def extract_trace_context(carrier: dict[str, str]) -> Context:
    """Read a span context out of a carrier, for use as the parent of a span."""
    return _propagator.extract(carrier)


def current_traceparent() -> str | None:
    """The ``traceparent`` of the current span, or nothing while tracing is off.

    Stored with a row that is picked up later, outside the current trace: an
    outbox record, a queued notification.
    """
    return inject_trace_context({}).get(TRACEPARENT_HEADER)


def context_of(traceparent: str | None) -> Context:
    """A context whose span is the one ``traceparent`` names.

    Empty when there is none, which makes a span started in it a root.
    """
    if traceparent is None:
        return Context()
    return extract_trace_context({TRACEPARENT_HEADER: traceparent})


def links_to(traceparent: str | None) -> list[trace.Link]:
    """A link to the span ``traceparent`` names, for a span that is not its child."""
    span_context = trace.get_current_span(context_of(traceparent)).get_span_context()
    if not span_context.is_valid:
        return []
    return [trace.Link(span_context)]
