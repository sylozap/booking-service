"""The platform fuzzed through the gateway against its own OpenAPI document.

The five services run in this process on uvicorn, on ports of localhost, over
PostgreSQL, Redis and Kafka of testcontainers, each on a database of its own
that nothing else uses: the fuzzer creates salons, masters and accounts out of
whatever the schema allows, and that garbage stays here. The seed fills the
catalog first, so that lists come back with items to check and not empty.

schemathesis reads ``/openapi.json`` of the gateway -- the merged document a
client sees -- and sends what it generates through the gateway, as a super
administrator, so that closed operations are reached past ``401``. Valid and
invalid inputs alike. What it holds the answers to:

- no ``500``, on any input;
- the status code is one the operation documents;
- the body matches the documented schema and content type, headers included.

A failure is fixed where it is, in the service or in its schema, and never
silenced here.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr
from seed import SeedSettings, seed

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
from barber_common.testing.servers import free_port, private_key_pem, serve
from barber_gateway.main import create_application as create_gateway
from barber_gateway.settings import GatewaySettings
from barber_notification.main import create_application as create_notification
from barber_notification.settings import ALEMBIC_INI as NOTIFICATION_ALEMBIC_INI
from barber_notification.settings import NotificationSettings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.fuzz,
    pytest.mark.asyncio(loop_scope="module"),
]

ISSUER = "https://barber.local/auth"
ADMIN_EMAIL = "admin@barber.example"
ADMIN_PASSWORD = "admin-password-of-the-fuzz-1"
BOOKING_CLIENT_SECRET = "booking-secret-of-the-fuzz"
SERVICES = ("auth", "catalog", "booking", "notification")

CHECKS = (
    "not_a_server_error",
    "status_code_conformance",
    "content_type_conformance",
    "response_schema_conformance",
    "response_headers_conformance",
)
# Examples per operation and phase. FUZZ_MAX_EXAMPLES raises it for a deeper
# run by hand; the default keeps the test within a few minutes.
MAX_EXAMPLES = int(os.environ.get("FUZZ_MAX_EXAMPLES", "25"))
# Fixed: a failure found in CI is found again on a laptop.
SEED = 20261016


@dataclass(frozen=True)
class Platform:
    gateway_url: str
    seed: SeedSettings


@pytest.fixture(scope="module")
def databases(postgres_dsn: str) -> dict[str, str]:
    """A database per service, migrated to head, used by this module alone."""
    dsns = {}
    for name, alembic_ini in zip(
        SERVICES,
        (AUTH_ALEMBIC_INI, CATALOG_ALEMBIC_INI, BOOKING_ALEMBIC_INI, NOTIFICATION_ALEMBIC_INI),
        strict=True,
    ):
        dsns[name] = create_database(postgres_dsn, f"{name}_fuzz")
        apply_migrations(dsn=dsns[name], alembic_ini=alembic_ini)
    return dsns


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def platform(
    databases: dict[str, str], redis_dsn: str, kafka_bootstrap: str
) -> AsyncIterator[Platform]:
    """The five services, listening, and the gateway in front of them."""
    ports = {name: free_port() for name in (*SERVICES, "gateway")}
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
            password_argon2_time_cost=1,
            password_argon2_memory_kib=8,
            password_argon2_parallelism=1,
            # The token of the run outlives the run.
            access_token_ttl_minutes=180,
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
    notification = create_notification(
        NotificationSettings(
            **shared, service_name="notification", database_dsn=databases["notification"]
        )
    )
    gateway = create_gateway(
        GatewaySettings(
            **shared,
            auth_url=url["auth"],
            catalog_url=url["catalog"],
            booking_url=url["booking"],
            notification_url=url["notification"],
            # The fuzzer is one client sending thousands of requests; a limit
            # it runs into turns every later check into a check of 429.
            rate_limit_anonymous_per_minute=1_000_000,
            rate_limit_user_per_minute=1_000_000,
            rate_limit_booking_create_per_minute=1_000_000,
            rate_limit_registration_per_hour=1_000_000,
        )
    )

    # auth first: the others fetch its keys when they start.
    running = await serve({ports["auth"]: auth})
    running += await serve(
        {
            ports["catalog"]: catalog,
            ports["booking"]: booking,
            ports["notification"]: notification,
        }
    )
    running += await serve({ports["gateway"]: gateway})

    yield Platform(
        gateway_url=url["gateway"],
        seed=SeedSettings(
            auth_url=url["auth"],
            catalog_url=url["catalog"],
            booking_url=url["booking"],
            admin_email=ADMIN_EMAIL,
            admin_password=SecretStr(ADMIN_PASSWORD),
            ready_timeout_seconds=60,
        ),
    )

    await running.stop()


async def admin_token(platform: Platform) -> str:
    async with httpx.AsyncClient(base_url=platform.gateway_url) as gateway:
        response = await gateway.post(
            "/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
        )
    response.raise_for_status()
    token: str = response.json()["access_token"]
    return token


async def known_identifiers(platform: Platform, token: str) -> dict[str, list[str]]:
    """Identifiers of what the seed created, by the name of the path parameter."""
    found: dict[str, list[str]] = {
        "salon_id": [],
        "master_id": [],
        "service_id": [],
        "booking_id": [],
    }
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=platform.gateway_url, headers=headers) as gateway:

        async def ids(path: str) -> list[str]:
            response = await gateway.get(path, params={"limit": "100"})
            response.raise_for_status()
            return [item["id"] for item in response.json()["items"]]

        for salon in await ids("/api/v1/salons"):
            found["salon_id"].append(salon)
            found["master_id"] += await ids(f"/api/v1/salons/{salon}/masters")
            found["service_id"] += await ids(f"/api/v1/salons/{salon}/services")
            found["booking_id"] += (await ids(f"/api/v1/bookings?salon_id={salon}"))[:10]
    return found


def configuration(identifiers: dict[str, list[str]], path: Path) -> Path:
    """A schemathesis.toml that draws path identifiers mostly from what exists.

    A random UUID is a 404 before any logic runs; without real identifiers the
    operations on one salon, master, service or booking would be tested only
    as far as the lookup. One draw in five stays random, so 404 is still met.
    """
    lines = []
    for name, values in identifiers.items():
        listed = ", ".join(f'"{value}"' for value in values)
        lines += [f"[dictionaries.{name}]", f"values = [{listed}]", ""]
    lines.append("[parameters]")
    for name in identifiers:
        lines.append(f'"path.{name}" = {{ dictionary = "{name}", probability = 0.8 }}')
    path.write_text("\n".join(lines) + "\n")
    return path


async def test_no_answer_breaks_the_published_contract(platform: Platform, tmp_path: Path) -> None:
    await seed(platform.seed)
    token = await admin_token(platform)
    identifiers = await known_identifiers(platform, token)
    assert all(identifiers.values()), identifiers

    command = [
        sys.executable,
        "-m",
        "schemathesis.cli",
        "--config-file",
        str(configuration(identifiers, tmp_path / "schemathesis.toml")),
        "run",
        f"{platform.gateway_url}/openapi.json",
        "--checks",
        ",".join(CHECKS),
        "--mode",
        "all",
        "--phases",
        "examples,coverage,fuzzing",
        "--max-examples",
        str(MAX_EXAMPLES),
        "--seed",
        str(SEED),
        "--header",
        f"Authorization: Bearer {token}",
        "--request-timeout",
        "15",
        "--continue-on-failure",
        "--report",
        "junit",
        "--report-dir",
        str(tmp_path),
        "--no-color",
    ]
    process = await asyncio.create_subprocess_exec(
        *command,
        # Its cache of examples goes to .schemathesis/ of the working directory.
        cwd=tmp_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()

    report = output.decode(errors="replace")
    assert process.returncode == 0, report
