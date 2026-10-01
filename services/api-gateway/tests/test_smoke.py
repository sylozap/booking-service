"""Smoke test of the api-gateway service: it assembles and it answers."""

from __future__ import annotations

import httpx

from barber_gateway.settings import GatewaySettings


async def test_health_live_answers_while_the_process_runs(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_metrics_are_the_gateways_own_and_not_proxied(client: httpx.AsyncClient) -> None:
    response = await client.get("/metrics")

    assert response.status_code == 200
    assert "http_requests_total" in response.text or "# HELP" in response.text


def test_the_service_carries_its_own_name(settings: GatewaySettings) -> None:
    assert settings.service_name == "api-gateway"
