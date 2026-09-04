"""Tracing has to be invisible while it is off and complete while it is on."""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest
from fastapi import APIRouter
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util._once import Once

from barber_common.app import create_app
from barber_common.config import BaseAppSettings
from barber_common.tracing import configure_tracing

router = APIRouter()


@router.get("/bookings/{booking_id}")
async def get_booking(booking_id: str) -> dict[str, str]:
    return {"booking_id": booking_id}


def reset_tracing() -> None:
    """Forget the provider of the process.

    OpenTelemetry installs it once and refuses to replace it, which is right in
    a service and useless in a test suite that builds several applications.
    There is no public way back, so the private slots are cleared here.
    """
    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE = Once()


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    reset_tracing()
    yield InMemorySpanExporter()
    reset_tracing()


def flush_spans() -> None:
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    provider.force_flush()


def server_spans(exporter: InMemorySpanExporter) -> list[ReadableSpan]:
    """Only the spans of requests this service handled.

    The client that drives the test is instrumented too, so its outgoing spans
    end up in the same exporter and say nothing about the service.
    """
    return [span for span in exporter.get_finished_spans() if span.kind is trace.SpanKind.SERVER]


def read_records(captured: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in captured.splitlines() if line.strip()]


async def test_application_works_while_tracing_is_disabled(
    settings: BaseAppSettings, exporter: InMemorySpanExporter
) -> None:
    app = create_app(settings, routers=[router])

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/bookings/b-1")

    assert response.status_code == 200
    assert exporter.get_finished_spans() == ()


async def test_trace_id_is_null_in_the_log_while_tracing_is_disabled(
    settings: BaseAppSettings,
    exporter: InMemorySpanExporter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = create_app(settings, routers=[router])

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/bookings/b-1")

    records = read_records(capsys.readouterr().out)

    assert all(record["trace_id"] is None for record in records)


async def test_request_produces_a_span(
    settings: BaseAppSettings, exporter: InMemorySpanExporter
) -> None:
    configure_tracing(settings, span_exporter=exporter)
    app = create_app(settings, routers=[router])

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/bookings/b-1")
    flush_spans()

    spans = server_spans(exporter)

    assert len(spans) == 1
    assert spans[0].name == "GET /bookings/{booking_id}"


async def test_trace_id_in_the_log_is_the_id_of_the_span(
    settings: BaseAppSettings,
    exporter: InMemorySpanExporter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_tracing(settings, span_exporter=exporter)
    app = create_app(settings, routers=[router])

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/bookings/b-1")
    flush_spans()

    span = server_spans(exporter)[0]
    assert span.context is not None
    handled = [
        record
        for record in read_records(capsys.readouterr().out)
        if record["event"] == "request handled"
    ]

    assert len(handled) == 1
    assert handled[0]["trace_id"] == trace.format_trace_id(span.context.trace_id)


async def test_probes_do_not_produce_spans(
    settings: BaseAppSettings, exporter: InMemorySpanExporter
) -> None:
    configure_tracing(settings, span_exporter=exporter)
    app = create_app(settings)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/health/live")
    flush_spans()

    assert server_spans(exporter) == []
