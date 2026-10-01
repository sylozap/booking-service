"""The document of the platform, merged from the real services.

Every service is built in process from its own application factory, nothing
running behind it: an OpenAPI document needs no database. The gateway collects
their documents exactly as it would over the network, through an ASGI
transport instead of a socket.

A test that imports every service, like the one in ``tests/e2e``, because what
it checks is the agreement between them and the gateway, which no suite of a
single service can see:

* the merged document is valid OpenAPI and every operation documents its
  errors;
* the routing table of the gateway sends each path to the service that
  describes it;
* the whitelist of the gateway opens exactly the operations the services
  declare open, and closes the rest.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from openapi_spec_validator import validate
from pydantic import SecretStr

from barber_auth.main import create_application as create_auth
from barber_auth.settings import AuthSettings
from barber_booking.main import create_application as create_booking
from barber_booking.settings import BookingSettings
from barber_catalog.main import create_application as create_catalog
from barber_catalog.settings import CatalogSettings
from barber_common.config import Environment
from barber_common.errors import PROBLEM_CONTENT_TYPE
from barber_common.http import ServiceClient
from barber_common.testing.fixtures import app_client
from barber_gateway.auth import is_public
from barber_gateway.clients.openapi import OpenApiClient
from barber_gateway.main import create_application as create_gateway
from barber_gateway.openapi import GATEWAY_SOURCE, PlatformDocument
from barber_gateway.routing import Upstream, route_for
from barber_gateway.settings import GatewaySettings
from barber_notification.main import create_application as create_notification
from barber_notification.settings import NotificationSettings

HTTP_METHODS = {"get", "put", "post", "delete", "patch", "head", "options"}

SHARED: dict[str, object] = {
    "environment": Environment.TEST,
    "redis_dsn": "redis://localhost:6379/0",
    "kafka_bootstrap_servers": "localhost:9092",
}


def _dsn(name: str) -> str:
    return f"postgresql+asyncpg://{name}:secret@localhost:5432/{name}"


def _signing_key_pem() -> SecretStr:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return SecretStr(pem.decode("ascii"))


def _services() -> dict[Upstream, FastAPI]:
    return {
        Upstream.AUTH: create_auth(
            AuthSettings.model_validate(
                {**SHARED, "database_dsn": _dsn("auth"), "jwt_private_key": _signing_key_pem()}
            )
        ),
        Upstream.CATALOG: create_catalog(
            CatalogSettings.model_validate({**SHARED, "database_dsn": _dsn("catalog")})
        ),
        Upstream.BOOKING: create_booking(
            BookingSettings.model_validate(
                {
                    **SHARED,
                    "database_dsn": _dsn("booking"),
                    "catalog_url": "http://catalog",
                    "auth_url": "http://auth",
                    "service_client_secret": "secret",
                }
            )
        ),
        Upstream.NOTIFICATION: create_notification(
            NotificationSettings.model_validate({**SHARED, "database_dsn": _dsn("notification")})
        ),
    }


@pytest.fixture(scope="module")
def services() -> dict[Upstream, FastAPI]:
    return _services()


@pytest.fixture
async def document(services: dict[Upstream, FastAPI]) -> AsyncIterator[dict[str, Any]]:
    """The document the gateway serves at /openapi.json."""
    gateway = create_gateway(
        GatewaySettings.model_validate(
            {
                **SHARED,
                "auth_url": "http://auth",
                "catalog_url": "http://catalog",
                "booking_url": "http://booking",
                "notification_url": "http://notification",
            }
        )
    )
    clients = {
        upstream: ServiceClient(
            base_url=f"http://{upstream.value}",
            upstream=upstream.value,
            transport=httpx.ASGITransport(app=app),
        )
        for upstream, app in services.items()
    }
    gateway.state.openapi = PlatformDocument(
        gateway=gateway.openapi, documents=OpenApiClient(http=clients), ttl_seconds=60
    )

    async with app_client(gateway) as client:
        merged: dict[str, Any] = (await client.get("/openapi.json")).json()
    for client_of_service in clients.values():
        await client_of_service.aclose()

    yield merged


def _operations(document: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (method, path, operation)
        for path, item in document["paths"].items()
        for method, operation in item.items()
        if method in HTTP_METHODS
    ]


def _source(operation: dict[str, Any]) -> str:
    """The service an operation came from, read off its prefixed tag."""
    tag: str = operation["tags"][0]
    return tag.split(" · ")[0]


def test_every_service_is_in_the_document(document: dict[str, Any]) -> None:
    sources = {_source(operation) for _, _, operation in _operations(document)}

    assert "x-missing-services" not in document["info"]
    assert sources == {GATEWAY_SOURCE, *(upstream.value for upstream in Upstream)}


def test_the_document_is_valid_openapi(document: dict[str, Any]) -> None:
    validate(document)


def test_internal_endpoints_are_not_published(document: dict[str, Any]) -> None:
    assert not [path for path in document["paths"] if path.startswith("/internal/")]


def test_every_operation_has_a_description_and_documents_its_errors(
    document: dict[str, Any],
) -> None:
    undocumented = [
        f"{method.upper()} {path}"
        for method, path, operation in _operations(document)
        if not operation.get("description")
        or not any(status[0] in "45" for status in operation["responses"])
    ]

    assert undocumented == []


def test_every_error_is_documented_as_a_problem_document(document: dict[str, Any]) -> None:
    wrong = [
        f"{method.upper()} {path} {status}"
        for method, path, operation in _operations(document)
        for status, response in operation["responses"].items()
        if status[0] in "45" and set(response.get("content", {})) != {PROBLEM_CONTENT_TYPE}
    ]

    assert wrong == []


def test_the_routing_table_sends_each_path_to_the_service_that_describes_it(
    document: dict[str, Any],
) -> None:
    misrouted = [
        f"{method.upper()} {path}: described by {_source(operation)}, routed to {route_for(path)}"
        for method, path, operation in _operations(document)
        if _source(operation) != GATEWAY_SOURCE and route_for(path) != Upstream(_source(operation))
    ]

    assert misrouted == []


def test_the_whitelist_of_the_gateway_matches_what_the_services_leave_open(
    document: dict[str, Any],
) -> None:
    disagreements = [
        f"{method.upper()} {path}: gateway public={is_public(method, path)}"
        for method, path, operation in _operations(document)
        if is_public(method, path) == bool(operation.get("security"))
    ]

    assert disagreements == []
