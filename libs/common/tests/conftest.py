"""Fixtures shared by the chassis tests."""

from __future__ import annotations

from collections.abc import Iterator

import docker
import pytest
from docker.errors import DockerException
from testcontainers.community.kafka import KafkaContainer
from testcontainers.community.postgres import PostgresContainer

from barber_common.config import BaseAppSettings, Environment

POSTGRES_IMAGE = "postgres:17-alpine"
# The default image of testcontainers, pinned so a pull never surprises CI.
KAFKA_IMAGE = "confluentinc/cp-kafka:7.6.0"


def _docker_is_available() -> bool:
    """Report whether a Docker daemon answers on this machine.

    Integration tests are skipped instead of failing when it does not: a laptop
    without Docker must still be able to run the unit tests. CI has Docker, so
    the tests do run where it matters.
    """
    try:
        docker.from_env().ping()
    except (DockerException, OSError):
        return False
    return True


@pytest.fixture(scope="session")
def postgres_dsn() -> Iterator[str]:
    """Start PostgreSQL once per session and hand out its async DSN."""
    if not _docker_is_available():
        pytest.skip("Docker is not available, integration tests need testcontainers")

    with PostgresContainer(POSTGRES_IMAGE, driver="asyncpg") as container:
        yield container.get_connection_url()


@pytest.fixture(scope="session")
def kafka_bootstrap() -> Iterator[str]:
    """Start Kafka once per session and hand out its bootstrap address."""
    if not _docker_is_available():
        pytest.skip("Docker is not available, integration tests need testcontainers")

    # KRaft, the same mode the platform runs in: no ZooKeeper anywhere.
    with KafkaContainer(KAFKA_IMAGE).with_kraft() as container:
        yield str(container.get_bootstrap_server())


@pytest.fixture
def settings() -> BaseAppSettings:
    """Settings of a service under test, with no environment behind them."""
    return BaseAppSettings(
        service_name="booking",
        environment=Environment.TEST,
        database_dsn="postgresql+asyncpg://booking:secret@localhost:5432/booking",
        redis_dsn="redis://localhost:6379/0",
        kafka_bootstrap_servers="localhost:9092",
    )
