"""The seed against the real auth, catalog and booking.

The three services run in this process on uvicorn, on ports of localhost, over
PostgreSQL, Redis and Kafka of testcontainers -- the same applications the
images run, talking HTTP to each other as in a cluster: booking asks catalog
with a service token from auth, catalog and booking verify tokens against the
keys of auth. Nothing of the platform is replaced; the seed is the only client.

The seed runs twice. The first run creates the demo, the second must find all
of it and create nothing.
"""

from __future__ import annotations

import asyncio
import socket
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from pydantic import SecretStr
from seed import BOOKING_DAYS, Report, SeedSettings, seed

from barber_auth.main import create_application as create_auth
from barber_auth.settings import ALEMBIC_INI as AUTH_ALEMBIC_INI
from barber_auth.settings import AuthSettings
from barber_booking.main import create_application as create_booking
from barber_booking.settings import ALEMBIC_INI as BOOKING_ALEMBIC_INI
from barber_booking.settings import BookingSettings
from barber_catalog.main import create_application as create_catalog
from barber_catalog.settings import ALEMBIC_INI as CATALOG_ALEMBIC_INI
from barber_catalog.settings import CatalogSettings
from barber_common.config import Environment
from barber_common.testing.fixtures import apply_migrations, create_database

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

ISSUER = "https://barber.local/auth"
ADMIN_EMAIL = "admin@barber.example"
ADMIN_PASSWORD = "admin-password-of-the-test-1"
BOOKING_CLIENT_SECRET = "booking-secret-of-the-test"
# Statuses that hold the time of a master: the partial EXCLUDE constraint.
ACTIVE = {"pending", "confirmed", "completed", "no_show"}


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


@dataclass
class Running:
    servers: list[uvicorn.Server]
    tasks: list[asyncio.Task[None]]


async def serve(apps: dict[int, FastAPI]) -> Running:
    """Start each application on its port and wait until all of them listen."""
    servers = [
        uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        for port, app in apps.items()
    ]
    tasks = [asyncio.create_task(server.serve()) for server in servers]
    async with asyncio.timeout(60):
        while not all(server.started for server in servers):
            if any(task.done() for task in tasks):
                [task.result() for task in tasks if task.done()]
            await asyncio.sleep(0.1)
    return Running(servers, tasks)


@pytest.fixture(scope="module")
def databases(postgres_dsn: str) -> dict[str, str]:
    """A database per service, migrated to head. Synchronous: the helpers run
    their own event loop, which cannot happen inside the loop of the tests."""
    dsns = {}
    for name, alembic_ini in (
        ("auth", AUTH_ALEMBIC_INI),
        ("catalog", CATALOG_ALEMBIC_INI),
        ("booking", BOOKING_ALEMBIC_INI),
    ):
        dsns[name] = create_database(postgres_dsn, f"{name}_seed")
        apply_migrations(dsn=dsns[name], alembic_ini=alembic_ini)
    return dsns


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def platform(
    databases: dict[str, str], redis_dsn: str, kafka_bootstrap: str
) -> AsyncIterator[SeedSettings]:
    """auth, catalog and booking, listening; the settings the seed needs to reach them."""
    ports = {name: free_port() for name in ("auth", "catalog", "booking")}
    url = {name: f"http://127.0.0.1:{port}" for name, port in ports.items()}

    shared: dict[str, Any] = {
        "environment": Environment.TEST,
        "redis_dsn": redis_dsn,
        "kafka_bootstrap_servers": kafka_bootstrap,
        "jwt_issuer": ISSUER,
        "jwks_url": url["auth"],
    }
    auth = create_auth(
        AuthSettings(
            **shared,
            service_name="auth",
            database_dsn=databases["auth"],
            jwt_private_key=SecretStr(private_key_pem()),
            # Cheap argon2: a hundred registrations at the real cost are a
            # test of the CPU, not of the seed.
            password_argon2_time_cost=1,
            password_argon2_memory_kib=8,
            password_argon2_parallelism=1,
            service_clients={
                "booking": {"secret": BOOKING_CLIENT_SECRET, "scopes": ["catalog:read"]}
            },
            bootstrap_admin_email=ADMIN_EMAIL,
            bootstrap_admin_phone="+79990000000",
            bootstrap_admin_password=SecretStr(ADMIN_PASSWORD),
        )
    )
    catalog = create_catalog(
        CatalogSettings(**shared, service_name="catalog", database_dsn=databases["catalog"])
    )
    booking = create_booking(
        BookingSettings(
            **shared,
            service_name="booking",
            database_dsn=databases["booking"],
            catalog_url=url["catalog"],
            auth_url=url["auth"],
            service_client_id="booking",
            service_client_secret=SecretStr(BOOKING_CLIENT_SECRET),
        )
    )

    # auth first, as in the cluster: catalog and booking fetch its keys when
    # they start, and a fetch that fails is not retried for ten seconds
    # (JWKS_REFRESH_MIN_INTERVAL_SECONDS) -- long enough for the seed to arrive
    # with a token they cannot verify yet.
    first = await serve({ports["auth"]: auth})
    rest = await serve({ports["catalog"]: catalog, ports["booking"]: booking})
    running = Running(first.servers + rest.servers, first.tasks + rest.tasks)

    yield SeedSettings(
        auth_url=url["auth"],
        catalog_url=url["catalog"],
        booking_url=url["booking"],
        admin_email=ADMIN_EMAIL,
        admin_password=SecretStr(ADMIN_PASSWORD),
        ready_timeout_seconds=60,
    )

    for server in running.servers:
        server.should_exit = True
    await asyncio.gather(*running.tasks)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def first_run(platform: SeedSettings) -> Report:
    return await seed(platform)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def second_run(platform: SeedSettings, first_run: Report) -> Report:
    return await seed(platform)


async def as_admin(settings: SeedSettings) -> dict[str, str]:
    async with httpx.AsyncClient(base_url=settings.auth_url) as auth:
        response = await auth.post(
            "/api/v1/auth/login",
            json={
                "email": settings.admin_email,
                "password": settings.admin_password.get_secret_value(),
            },
        )
    response.raise_for_status()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def every(
    base_url: str, path: str, headers: dict[str, str] | None = None, **params: str
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor = None
    async with httpx.AsyncClient(base_url=base_url, headers=headers) as client:
        while True:
            query = {"limit": "100", **params, **({"cursor": cursor} if cursor else {})}
            response = await client.get(path, params=query)
            response.raise_for_status()
            page = response.json()
            items += page["items"]
            cursor = page.get("next_cursor")
            if not cursor:
                return items


async def bookings(settings: SeedSettings) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    return await every(
        settings.booking_url,
        "/api/v1/bookings",
        headers=await as_admin(settings),
        **{"from": now.isoformat(), "to": (now + timedelta(days=BOOKING_DAYS + 2)).isoformat()},
    )


# --- the first run ------------------------------------------------------------


async def test_first_run_creates_the_whole_demo(first_run: Report) -> None:
    assert first_run.created["salons"] == 3
    assert first_run.created["services"] == 15
    assert first_run.created["masters"] == 12
    assert first_run.created["schedules"] == 12
    assert first_run.created["clients"] == 100
    assert first_run.created["bookings"] > 12 * 6
    assert first_run.found == {}


async def test_catalog_shows_three_salons(platform: SeedSettings, first_run: Report) -> None:
    salons = await every(platform.catalog_url, "/api/v1/salons")

    assert len(salons) == 3
    assert len({salon["timezone"] for salon in salons}) == 3


async def test_bookings_of_one_master_never_overlap(
    platform: SeedSettings, first_run: Report
) -> None:
    by_master: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    for booking in await bookings(platform):
        if booking["status"] in ACTIVE:
            occupied_until = datetime.fromisoformat(booking["end_at"]) + timedelta(
                minutes=booking["buffer_min"]
            )
            by_master[booking["master_id"]].append(
                (datetime.fromisoformat(booking["start_at"]), occupied_until)
            )

    assert len(by_master) == 12
    for intervals in by_master.values():
        intervals.sort()
        for (_, end), (start, _) in zip(intervals, intervals[1:], strict=False):
            assert end <= start


async def test_some_bookings_are_cancelled_by_the_salon(
    platform: SeedSettings, first_run: Report
) -> None:
    statuses = [booking["status"] for booking in await bookings(platform)]

    assert statuses.count("cancelled_by_salon") == first_run.created["cancellations"]
    assert first_run.created["cancellations"] >= 1


# --- the second run -----------------------------------------------------------


async def test_second_run_creates_nothing(second_run: Report) -> None:
    assert second_run.created == {}
    assert second_run.found["salons"] == 3
    assert second_run.found["services"] == 15
    assert second_run.found["masters"] == 12
    assert second_run.found["clients"] == 100


async def test_second_run_leaves_the_catalog_and_the_bookings_as_they_were(
    platform: SeedSettings, first_run: Report, second_run: Report
) -> None:
    salons = await every(platform.catalog_url, "/api/v1/salons")
    made = await bookings(platform)

    assert len(salons) == 3
    assert len(made) == first_run.created["bookings"]
