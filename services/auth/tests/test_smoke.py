"""Smoke test of the auth service: it assembles and it answers."""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI

from barber_auth.main import create_application
from barber_auth.settings import AuthSettings
from barber_common.config import Environment
from barber_common.testing.fixtures import app_client

DSN = "postgresql+asyncpg://auth:secret@localhost:5432/auth"


def build_settings() -> AuthSettings:
    """Settings sufficient to assemble the service and nothing more.

    A signing key is among them because there is no default for one: a service
    that invented its own would issue tokens nobody else can verify. It is
    generated here rather than committed -- a PEM in the repository would be a
    signing key in git history.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return AuthSettings(
        environment=Environment.TEST,
        database_dsn=DSN,
        redis_dsn="redis://localhost:6379/0",
        kafka_bootstrap_servers="localhost:9092",
        jwt_private_key=key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("ascii"),
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
    assert response.json()["info"]["title"] == "Barber Auth"


def test_the_service_carries_its_own_name() -> None:
    assert build_settings().service_name == "auth"
