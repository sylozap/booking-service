"""Liveness must not depend on anything external. Readiness must name the culprit."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from barber_common.health import HealthRegistry, create_health_router


class DependencyIsDown(RuntimeError):
    """Raised by a check standing in for an unreachable dependency."""


async def succeeding_check() -> None:
    return None


async def failing_check() -> None:
    raise DependencyIsDown


async def hanging_check() -> None:
    await asyncio.sleep(30)


def build_client(registry: HealthRegistry, *, timeout_seconds: float = 2.0) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(create_health_router(registry, timeout_seconds=timeout_seconds))
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
async def registry() -> AsyncIterator[HealthRegistry]:
    yield HealthRegistry()


async def test_live_answers_200_without_any_registered_check(
    registry: HealthRegistry,
) -> None:
    async with build_client(registry) as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_live_answers_200_while_the_database_is_unreachable(
    registry: HealthRegistry,
) -> None:
    registry.register("database", failing_check)

    async with build_client(registry) as client:
        response = await client.get("/health/live")

    assert response.status_code == 200


async def test_ready_answers_200_when_every_check_passes(
    registry: HealthRegistry,
) -> None:
    registry.register("database", succeeding_check)
    registry.register("redis", succeeding_check)

    async with build_client(registry) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_ready_answers_503_and_names_the_failed_check(
    registry: HealthRegistry,
) -> None:
    registry.register("database", failing_check)
    registry.register("redis", succeeding_check)

    async with build_client(registry) as client:
        response = await client.get("/health/ready")

    failed = [check for check in response.json()["checks"] if check["status"] == "fail"]

    assert response.status_code == 503
    assert [check["name"] for check in failed] == ["database"]
    assert failed[0]["detail"] == "DependencyIsDown"


async def test_ready_reports_a_hanging_check_within_the_shared_deadline(
    registry: HealthRegistry,
) -> None:
    registry.register("kafka", hanging_check)

    started_at = time.perf_counter()
    async with build_client(registry, timeout_seconds=0.05) as client:
        response = await client.get("/health/ready")
    elapsed = time.perf_counter() - started_at

    assert response.status_code == 503
    assert response.json()["checks"][0]["detail"] == "timed out"
    assert elapsed < 1.0


async def test_checks_run_in_parallel_not_one_after_another(
    registry: HealthRegistry,
) -> None:
    async def slow_check() -> None:
        await asyncio.sleep(0.1)

    for name in ("database", "redis", "kafka"):
        registry.register(name, slow_check)

    started_at = time.perf_counter()
    async with build_client(registry) as client:
        response = await client.get("/health/ready")
    elapsed = time.perf_counter() - started_at

    assert response.status_code == 200
    assert elapsed < 0.3  # sequential execution would need at least 0.3


def test_registering_the_same_name_twice_is_a_mistake(registry: HealthRegistry) -> None:
    registry.register("database", succeeding_check)

    with pytest.raises(ValueError, match="database"):
        registry.register("database", succeeding_check)
