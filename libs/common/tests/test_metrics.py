"""An identifier must never reach a metric label."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from barber_common.metrics import REGISTRY, instrument_app

BOOKING_ROUTE = "/bookings/{booking_id}"


def build_app() -> FastAPI:
    app = FastAPI()

    @app.get(BOOKING_ROUTE)
    async def get_booking(booking_id: str) -> dict[str, str]:
        return {"booking_id": booking_id}

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("boom")

    instrument_app(app)
    return app


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=build_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def requests_total(path: str, status: str) -> float:
    value = REGISTRY.get_sample_value(
        "http_requests_total", {"method": "GET", "path": path, "status": status}
    )
    return value or 0.0


async def test_metrics_endpoint_answers_in_the_prometheus_format(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/metrics")

    assert response.status_code == 200
    assert "http_requests_total" in response.text


async def test_path_label_holds_the_route_template(client: httpx.AsyncClient) -> None:
    booking_id = str(uuid4())

    await client.get(f"/bookings/{booking_id}")
    metrics = (await client.get("/metrics")).text

    assert f'path="{BOOKING_ROUTE}"' in metrics
    assert booking_id not in metrics


async def test_counter_grows_after_a_request(client: httpx.AsyncClient) -> None:
    before = requests_total(BOOKING_ROUTE, "200")

    await client.get(f"/bookings/{uuid4()}")

    assert requests_total(BOOKING_ROUTE, "200") == before + 1


async def test_failed_request_is_counted_with_its_status(
    client: httpx.AsyncClient,
) -> None:
    before = requests_total("/boom", "500")

    await client.get("/boom")

    assert requests_total("/boom", "500") == before + 1


async def test_unknown_path_does_not_create_a_series_of_its_own(
    client: httpx.AsyncClient,
) -> None:
    await client.get("/no-such-path-42")

    metrics = (await client.get("/metrics")).text

    assert "no-such-path-42" not in metrics


async def test_duration_is_observed_without_the_status_label(
    client: httpx.AsyncClient,
) -> None:
    await client.get(f"/bookings/{uuid4()}")

    count = REGISTRY.get_sample_value(
        "http_request_duration_seconds_count",
        {"method": "GET", "path": BOOKING_ROUTE},
    )

    assert count is not None
    assert count >= 1
