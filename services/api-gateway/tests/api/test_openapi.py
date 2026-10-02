"""The document of the platform at /openapi.json of the gateway."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

import httpx
import pytest

from barber_gateway.routing import Upstream

Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]

PATHS = {
    Upstream.AUTH: "/api/v1/auth/login",
    Upstream.CATALOG: "/api/v1/salons",
    Upstream.BOOKING: "/api/v1/bookings",
    Upstream.NOTIFICATION: "/api/v1/notifications/preferences",
}


class FakeServices(Protocol):
    """The fake services of the conftest, as far as these tests use them.

    A protocol rather than the class itself: pytest runs this suite in
    importlib mode, so ``tests.conftest`` is not an importable module.
    """

    def answer(self, upstream: Upstream, handler: Handler) -> None: ...

    def reached(self, upstream: Upstream) -> list[httpx.Request]: ...


def serving_document(upstream: Upstream) -> Handler:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "openapi": "3.1.0",
                "info": {"title": upstream.value, "version": "1"},
                "paths": {
                    PATHS[upstream]: {
                        "get": {
                            "operationId": f"read_{upstream.value}",
                            "description": "Reads.",
                            "responses": {"200": {"description": "OK"}},
                        }
                    }
                },
            },
        )

    return handler


async def unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused")


@pytest.fixture
def documented(services: FakeServices) -> None:
    for upstream in Upstream:
        services.answer(upstream, serving_document(upstream))


@pytest.mark.usefixtures("documented")
async def test_the_document_holds_every_service_and_the_gateways_own_endpoint(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/openapi.json")

    paths = response.json()["paths"]
    assert response.status_code == 200
    assert set(PATHS.values()) <= set(paths)
    assert "/api/v1/masters/{master_id}/card" in paths
    assert "x-missing-services" not in response.json()["info"]


@pytest.mark.usefixtures("documented")
async def test_the_document_is_collected_once_and_served_from_memory(
    client: httpx.AsyncClient, services: FakeServices
) -> None:
    await client.get("/openapi.json")

    await client.get("/openapi.json")

    assert all(len(services.reached(upstream)) == 1 for upstream in Upstream)


@pytest.mark.usefixtures("documented")
async def test_a_service_that_did_not_answer_is_named_and_asked_again(
    client: httpx.AsyncClient, services: FakeServices
) -> None:
    services.answer(Upstream.BOOKING, unreachable)

    incomplete = await client.get("/openapi.json")
    services.answer(Upstream.BOOKING, serving_document(Upstream.BOOKING))
    complete = await client.get("/openapi.json")

    assert incomplete.status_code == 200
    assert incomplete.json()["info"]["x-missing-services"] == ["booking"]
    assert PATHS[Upstream.BOOKING] not in incomplete.json()["paths"]
    assert PATHS[Upstream.BOOKING] in complete.json()["paths"]
    assert "x-missing-services" not in complete.json()["info"]


@pytest.mark.usefixtures("documented")
async def test_swagger_ui_reads_the_document_of_the_platform(client: httpx.AsyncClient) -> None:
    response = await client.get("/docs")

    assert response.status_code == 200
    assert "/openapi.json" in response.text
