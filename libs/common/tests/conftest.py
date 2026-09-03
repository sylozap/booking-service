"""Fixtures shared by the chassis tests."""

from __future__ import annotations

from collections.abc import Iterator

import docker
import pytest
from docker.errors import DockerException
from testcontainers.community.postgres import PostgresContainer

POSTGRES_IMAGE = "postgres:17-alpine"


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
