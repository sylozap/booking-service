"""OpenTelemetry tracing across service boundaries.

Tracing is switched by ``OTLP_ENABLED``. While it is off, spans are
non-recording and the log fields ``trace_id`` and ``span_id`` stay ``null``.
Kafka is instrumented in the producer and the consumer, where the trace context
is written to and read from message headers.
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
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.semconv.resource import ResourceAttributes
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from sqlalchemy.ext.asyncio import AsyncEngine

from barber_common.config import BaseAppSettings
from barber_common.logging import get_logger

__all__ = [
    "TRACEPARENT_HEADER",
    "configure_tracing",
    "extract_trace_context",
    "inject_trace_context",
    "instrument_engine",
    "instrument_fastapi",
]

TRACEPARENT_HEADER = "traceparent"

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
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    # The outgoing side of every synchronous call between services.
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    _instrument_redis(provider)

    _logger.info("tracing is enabled", otlp_endpoint=settings.otlp_endpoint)


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
