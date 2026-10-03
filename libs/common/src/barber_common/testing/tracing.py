"""Spans recorded in memory, for the tests that follow a trace."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util._once import Once

__all__ = ["recorded_spans"]


@contextmanager
def recorded_spans() -> Iterator[InMemorySpanExporter]:
    """Record every finished span of the process for the duration of the block.

    The processor is synchronous, so a span is in the exporter the moment it
    ends and a test reads it without flushing.
    """
    _forget_provider()
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    try:
        yield exporter
    finally:
        provider.shutdown()
        _forget_provider()


def _forget_provider() -> None:
    """Uninstall the provider of the process.

    OpenTelemetry installs it once and refuses to replace it, which is right in
    a service and useless in a suite that installs one per test. There is no
    public way back, so the private slots are cleared.
    """
    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE = Once()
