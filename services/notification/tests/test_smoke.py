"""Smoke test of the notification service: it assembles and it answers."""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from barber_common.config import Environment
from barber_common.testing.fixtures import app_client
from barber_notification.main import create_application
from barber_notification.settings import NotificationSettings

DSN = "postgresql+asyncpg://notification:secret@localhost:5432/notification"


def build_settings() -> NotificationSettings:
    return NotificationSettings(
        environment=Environment.TEST,
        database_dsn=DSN,
        redis_dsn="redis://localhost:6379/0",
        kafka_bootstrap_servers="localhost:9092",
    )


@pytest.fixture
def app() -> FastAPI:
    """The real application, with nothing running behind it.

    The lifespan does not run here, so nothing connects to a database or a
    broker. This test answers "does the service assemble and route"; whether it
    can talk to its dependencies is the question of the compose stack.
    """
    return create_application(build_settings())


async def test_health_live_answers_while_the_process_runs(app: FastAPI) -> None:
    async with app_client(app) as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_the_openapi_document_is_generated(app: FastAPI) -> None:
    async with app_client(app) as client:
        response = await client.get("/openapi.json")

    assert response.status_code == 200
    assert response.json()["info"]["title"] == "Barber Notification"


def test_the_service_carries_its_own_name() -> None:
    assert build_settings().service_name == "notification"
