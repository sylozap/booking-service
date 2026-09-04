"""Containers, database isolation and the waiting helpers.

Three decisions shape everything here.

**Containers start once per session.** A test run that recreates PostgreSQL per
test is a run nobody waits for, and a suite nobody runs stops catching
regressions within a week.

**Isolation is a rollback, not a recreation.** Every test gets a session bound
to a connection with an open transaction; whatever it writes is rolled back
when it ends. Two tests writing to one table never see each other.

**The schema comes from the migrations.** ``create_all`` is not used anywhere,
including here: a schema built from the models would leave the migrations
untested until they run in production for the first time.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import docker
import httpx
import pytest
from aiokafka import AIOKafkaConsumer, ConsumerRecord
from alembic import command
from docker.errors import DockerException
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from testcontainers.community.kafka import KafkaContainer
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from barber_common.db.engine import create_engine
from barber_common.db.migrations import load_config

__all__ = [
    "app_client",
    "apply_migrations",
    "create_database",
    "docker_is_available",
    "isolated_session_factory",
    "kafka_bootstrap",
    "postgres_dsn",
    "read_events",
    "redis_dsn",
    "wait_for",
]

# Pinned so a pull never surprises CI with a new default.
POSTGRES_IMAGE = "postgres:17-alpine"
KAFKA_IMAGE = "confluentinc/cp-kafka:7.6.0"
REDIS_IMAGE = "redis:7-alpine"


def docker_is_available() -> bool:
    """Report whether a Docker daemon answers on this machine.

    Integration tests are skipped rather than failed when it does not: a laptop
    without Docker still runs the unit tests, and CI has Docker, so the tests
    do run where it matters.
    """
    try:
        docker.from_env().ping()
    except (DockerException, OSError):
        return False
    return True


def _require_docker() -> None:
    if not docker_is_available():
        pytest.skip("Docker is not available, integration tests need testcontainers")


@pytest.fixture(scope="session")
def postgres_dsn() -> Iterator[str]:
    """Start PostgreSQL once per session and hand out its async DSN."""
    _require_docker()
    with PostgresContainer(POSTGRES_IMAGE, driver="asyncpg") as container:
        yield container.get_connection_url()


@pytest.fixture(scope="session")
def kafka_bootstrap() -> Iterator[str]:
    """Start Kafka once per session and hand out its bootstrap address."""
    _require_docker()
    # KRaft, the same mode the platform runs in: no ZooKeeper anywhere.
    with KafkaContainer(KAFKA_IMAGE).with_kraft() as container:
        yield str(container.get_bootstrap_server())


@pytest.fixture(scope="session")
def redis_dsn() -> Iterator[str]:
    """Start Redis once per session and hand out its DSN."""
    _require_docker()
    with RedisContainer(REDIS_IMAGE) as container:
        yield f"redis://{container.get_container_host_ip()}:{container.get_exposed_port(6379)}/0"


def create_database(admin_dsn: str, name: str) -> str:
    """Create an empty database next to the one the container came with.

    Every suite that runs migrations gets its own: a downgrade in one test file
    would otherwise drop the tables another file is using, and the order of the
    files would decide whether the run is green.
    """
    dsn = _with_database(admin_dsn, name)
    asyncio.run(_recreate_database(admin_dsn, name))
    return dsn


async def _recreate_database(admin_dsn: str, name: str) -> None:
    # CREATE DATABASE cannot run inside a transaction, hence AUTOCOMMIT.
    engine = create_engine(admin_dsn, pool_size=1, max_overflow=0)
    try:
        async with engine.connect() as connection:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
            await connection.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await engine.dispose()


def _with_database(dsn: str, name: str) -> str:
    parts = urlsplit(dsn)
    return urlunsplit(parts._replace(path=f"/{name}"))


def apply_migrations(*, dsn: str, alembic_ini: Path | str, revision: str = "head") -> None:
    """Bring a database to a revision with the real migration scripts.

    Synchronous on purpose: Alembic runs its own event loop, so this belongs in
    a session-scoped fixture, not inside a running one. From an async test,
    call it through ``asyncio.to_thread``.
    """
    command.upgrade(load_config(alembic_ini, dsn=dsn), revision)


@asynccontextmanager
async def isolated_session_factory(
    engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a session factory whose writes are undone when the test ends.

    Every session it produces joins the one open transaction by opening a
    savepoint, so code under test can commit -- the relay and the consumers do
    -- while the outer rollback still takes everything away afterwards.
    """
    async with engine.connect() as connection:
        transaction = await connection.begin()
        factory = async_sessionmaker(
            bind=connection,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield factory
        finally:
            await transaction.rollback()


def app_client(app: FastAPI, *, base_url: str = "http://testserver") -> httpx.AsyncClient:
    """Build a client that talks to the application in process.

    No socket and no port: the request goes through the ASGI stack, so the
    middleware, the error handlers and the dependencies are the real ones.
    """
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url)


async def wait_for(
    condition: Callable[[], bool],
    *,
    timeout_seconds: float = 10.0,
    interval_seconds: float = 0.05,
) -> None:
    """Poll a condition until it holds or the deadline passes.

    The replacement for ``sleep`` in a test: a fixed sleep is either too short
    on a loaded CI machine or wasted time on a fast one.
    """
    async with asyncio.timeout(timeout_seconds):
        # ASYNC110 suggests waiting on an Event instead. There is none to wait
        # on: the condition changes in another connection, another task or
        # another process, and polling is the only way to see it happen.
        while not condition():  # noqa: ASYNC110
            await asyncio.sleep(interval_seconds)


async def read_events(
    *,
    bootstrap_servers: str,
    topic: str,
    count: int = 1,
    timeout_seconds: float = 20.0,
    group_id: str | None = None,
) -> list[ConsumerRecord[bytes, bytes]]:
    """Wait for messages to appear on a topic and return them.

    Reads from the beginning in a group of its own, so it sees what was
    published before it started and does not disturb the consumer under test.
    """
    consumer: AIOKafkaConsumer[bytes, bytes] = AIOKafkaConsumer(
        topic,
        bootstrap_servers=bootstrap_servers,
        group_id=group_id or f"test-reader-{topic}",
        auto_offset_reset="earliest",
        enable_auto_commit=False,
    )
    await consumer.start()
    records: list[ConsumerRecord[bytes, bytes]] = []
    try:
        async with asyncio.timeout(timeout_seconds):
            while len(records) < count:
                records.append(await consumer.getone())
    finally:
        await consumer.stop()
    return records
