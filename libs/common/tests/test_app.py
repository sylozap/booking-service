"""The chassis has to hold the correlation id and let a request finish."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import APIRouter

from barber_common.app import create_app
from barber_common.config import BaseAppSettings
from barber_common.errors import DomainError
from barber_common.middleware import CORRELATION_ID_HEADER

router = APIRouter()

SLOW_REQUEST_SECONDS = 0.2

# Set by the slow endpoint so the test knows the request reached it.
request_started = asyncio.Event()


class SlotAlreadyTaken(DomainError):
    code = "slot_taken"
    http_status = 409
    title = "Slot is already taken"


@router.get("/bookings")
async def list_bookings() -> list[str]:
    return []


@router.get("/slow")
async def slow() -> dict[str, str]:
    request_started.set()
    await asyncio.sleep(SLOW_REQUEST_SECONDS)
    return {"status": "done"}


@router.get("/taken")
async def taken() -> None:
    raise SlotAlreadyTaken("The master is busy")


@pytest.fixture
async def client(settings: BaseAppSettings) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings, routers=[router])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_correlation_id_of_the_caller_comes_back_in_the_response(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/bookings", headers={CORRELATION_ID_HEADER: "c-8f2a"})

    assert response.headers[CORRELATION_ID_HEADER] == "c-8f2a"


async def test_correlation_id_is_generated_when_the_caller_sends_none(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/bookings")

    assert response.headers[CORRELATION_ID_HEADER]


async def test_correlation_id_reaches_the_problem_document(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/taken", headers={CORRELATION_ID_HEADER: "c-8f2a"})

    assert response.status_code == 409
    assert response.json()["correlation_id"] == "c-8f2a"


async def test_probes_and_metrics_are_mounted(client: httpx.AsyncClient) -> None:
    live = await client.get("/health/live")
    ready = await client.get("/health/ready")
    metrics = await client.get("/metrics")

    assert live.status_code == 200
    assert ready.status_code == 200
    assert metrics.status_code == 200


async def test_probes_stay_out_of_the_public_schema(client: httpx.AsyncClient) -> None:
    schema = (await client.get("/openapi.json")).json()

    assert "/health/live" not in schema["paths"]
    assert "/metrics" not in schema["paths"]


async def test_shutdown_waits_for_a_request_in_flight(
    settings: BaseAppSettings,
) -> None:
    app = create_app(settings, routers=[router], drain_timeout_seconds=5.0)
    transport = httpx.ASGITransport(app=app)

    request_started.clear()

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        started_at = time.perf_counter()
        async with app.router.lifespan_context(app):
            request = asyncio.ensure_future(client.get("/slow"))
            await asyncio.wait_for(request_started.wait(), timeout=1.0)
        elapsed = time.perf_counter() - started_at

        assert request.done()
        assert (await request).status_code == 200
        assert elapsed >= SLOW_REQUEST_SECONDS


async def test_ready_reports_draining_after_shutdown_started(
    settings: BaseAppSettings,
) -> None:
    app = create_app(settings, routers=[router])
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            pass
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "draining"


async def test_service_lifespan_runs_around_the_application(
    settings: BaseAppSettings,
) -> None:
    events: list[str] = []

    from contextlib import asynccontextmanager

    from fastapi import FastAPI

    @asynccontextmanager
    async def service_lifespan(app: FastAPI) -> AsyncIterator[None]:
        events.append("started")
        yield
        events.append("stopped")

    app = create_app(settings, lifespan=service_lifespan)

    async with app.router.lifespan_context(app):
        assert events == ["started"]

    assert events == ["started", "stopped"]
