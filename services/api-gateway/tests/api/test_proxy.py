"""Proxying: the request reaches the right service, the answer comes back intact."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol

import httpx
import pytest
from fastapi import FastAPI
from starlette.types import Message, Receive

from barber_common.errors import PROBLEM_CONTENT_TYPE
from barber_gateway.routing import Upstream


class FakeServices(Protocol):
    """The fake services of the conftest, as far as these tests use them.

    A protocol rather than the class itself: pytest runs this suite in
    importlib mode, so ``tests.conftest`` is not an importable module.
    """

    requests: list[httpx.Request]

    def answer(
        self, upstream: Upstream, handler: Callable[[httpx.Request], Awaitable[httpx.Response]]
    ) -> None: ...

    def reached(self, upstream: Upstream) -> list[httpx.Request]: ...


MASTER = "0192f3c1-6a2b-7c3d-8e4f-5a6b7c8d9e0f"


async def test_a_request_reaches_the_service_of_its_path(
    client: httpx.AsyncClient, services: FakeServices
) -> None:
    response = await client.get(f"/api/v1/masters/{MASTER}/schedule")

    assert response.status_code == 200
    assert response.json()["upstream"] == "booking"
    assert response.json()["path"] == f"/api/v1/masters/{MASTER}/schedule"
    assert len(services.reached(Upstream.BOOKING)) == 1


async def test_the_query_and_the_body_reach_the_service_unchanged(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/v1/bookings?dry=1&tag=a%20b",
        content=b'{"start_at": "2026-10-02T10:00:00Z"}',
        headers={"Content-Type": "application/json"},
    )

    echoed = response.json()
    assert echoed["method"] == "POST"
    assert echoed["query"] == "dry=1&tag=a%20b"
    assert echoed["body"] == '{"start_at": "2026-10-02T10:00:00Z"}'


async def test_the_status_and_the_body_of_the_service_come_back_untouched(
    client: httpx.AsyncClient, services: FakeServices
) -> None:
    problem = {"code": "slot_taken", "status": 409, "alternatives": ["2026-10-02T11:00:00Z"]}

    async def conflict(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            content=json.dumps(problem).encode(),
            headers={"Content-Type": PROBLEM_CONTENT_TYPE},
        )

    services.answer(Upstream.BOOKING, conflict)

    response = await client.post("/api/v1/bookings", json={})

    assert response.status_code == 409
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    assert response.json() == problem


async def test_a_path_no_service_owns_is_a_404_of_the_gateway(
    client: httpx.AsyncClient, services: FakeServices
) -> None:
    response = await client.get("/api/v1/nothing-here")

    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    assert response.json()["code"] == "not_found"
    assert services.requests == []


async def test_an_internal_endpoint_is_not_reachable_from_outside(
    client: httpx.AsyncClient, services: FakeServices
) -> None:
    response = await client.post("/internal/v1/token", json={})

    assert response.status_code == 404
    assert services.requests == []


async def test_the_headers_of_the_client_reach_the_service(
    client: httpx.AsyncClient, services: FakeServices
) -> None:
    await client.get(
        "/api/v1/salons",
        headers={
            "Accept-Language": "ru",
            "Authorization": "Bearer something",
            "X-Correlation-Id": "c-123",
        },
    )

    forwarded = services.reached(Upstream.CATALOG)[0].headers
    assert forwarded["accept-language"] == "ru"
    assert forwarded["authorization"] == "Bearer something"
    assert forwarded["x-correlation-id"] == "c-123"
    assert forwarded["host"] == "catalog"
    assert forwarded["x-forwarded-host"] == "testserver"
    assert forwarded["x-forwarded-proto"] == "http"
    assert forwarded["x-forwarded-for"] == "127.0.0.1"


async def test_the_gateway_mints_a_correlation_id_the_service_receives(
    client: httpx.AsyncClient, services: FakeServices
) -> None:
    response = await client.get("/api/v1/salons")

    minted = response.headers["x-correlation-id"]
    assert services.reached(Upstream.CATALOG)[0].headers["x-correlation-id"] == minted


async def test_hop_by_hop_headers_stop_at_the_gateway(
    client: httpx.AsyncClient, services: FakeServices
) -> None:
    await client.get(
        "/api/v1/salons",
        headers={"Connection": "keep-alive, X-Private-Hop", "X-Private-Hop": "1", "TE": "trailers"},
    )

    forwarded = services.reached(Upstream.CATALOG)[0].headers
    assert "x-private-hop" not in forwarded
    assert "te" not in forwarded
    assert forwarded.get("connection") != "keep-alive, X-Private-Hop"


async def test_the_headers_of_the_service_reach_the_client(
    client: httpx.AsyncClient, services: FakeServices
) -> None:
    async def with_headers(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={},
            headers=[
                ("Location", "/api/v1/bookings/1"),
                ("Set-Cookie", "a=1"),
                ("Set-Cookie", "b=2"),
                ("X-Correlation-Id", "from-the-service"),
            ],
        )

    services.answer(Upstream.BOOKING, with_headers)

    response = await client.post("/api/v1/bookings", json={}, headers={"X-Correlation-Id": "c-1"})

    assert response.status_code == 201
    assert response.headers["location"] == "/api/v1/bookings/1"
    assert response.headers.get_list("set-cookie") == ["a=1", "b=2"]
    # One id, the gateway's, rather than two disagreeing ones.
    assert response.headers.get_list("x-correlation-id") == ["c-1"]


async def test_a_service_that_does_not_answer_in_time_is_a_504(
    client: httpx.AsyncClient, services: FakeServices
) -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    services.answer(Upstream.BOOKING, slow)

    response = await client.get("/api/v1/bookings")

    assert response.status_code == 504
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    assert response.json()["code"] == "upstream_timeout"
    assert response.json()["upstream"] == "booking"


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("connection refused"),
        httpx.ConnectTimeout("connect timed out"),
    ],
)
async def test_a_service_that_cannot_be_reached_is_a_503(
    client: httpx.AsyncClient, services: FakeServices, failure: httpx.TransportError
) -> None:
    async def unreachable(request: httpx.Request) -> httpx.Response:
        raise failure

    services.answer(Upstream.CATALOG, unreachable)

    response = await client.get("/api/v1/salons")

    assert response.status_code == 503
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    assert response.json()["code"] == "upstream_unavailable"


async def test_an_open_breaker_spares_the_service_further_requests(
    client: httpx.AsyncClient, services: FakeServices
) -> None:
    async def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    services.answer(Upstream.CATALOG, unreachable)
    for _ in range(5):
        await client.get("/api/v1/salons")

    response = await client.get("/api/v1/salons")

    assert response.status_code == 503
    assert len(services.reached(Upstream.CATALOG)) == 5


async def test_an_error_status_of_a_service_does_not_open_its_breaker(
    client: httpx.AsyncClient, services: FakeServices
) -> None:
    async def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"code": "upstream_unavailable"})

    services.answer(Upstream.BOOKING, failing)
    for _ in range(10):
        await client.get("/api/v1/bookings")

    assert len(services.reached(Upstream.BOOKING)) == 10


async def test_a_large_answer_is_relayed_before_the_service_has_finished_it(
    app: FastAPI, services: FakeServices
) -> None:
    """The first chunk leaves the gateway while the service still holds the rest.

    The service sends one chunk and then waits until the client has received
    it. A gateway that buffered the answer whole would never let it through,
    and the service would give up waiting.
    """
    first_chunk_delivered = asyncio.Event()
    chunks = 50
    chunk = b"x" * 64 * 1024

    async def body() -> AsyncIterator[bytes]:
        yield chunk
        async with asyncio.timeout(5):
            await first_chunk_delivered.wait()
        for _ in range(chunks - 1):
            yield chunk

    async def large(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body())

    services.answer(Upstream.BOOKING, large)
    received: list[bytes] = []

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            received.append(message["body"])
            first_chunk_delivered.set()

    await app(_scope("/api/v1/bookings"), _receive_nothing(), send)

    assert len(received) > 1
    assert b"".join(received) == chunk * chunks


def _scope(path: str) -> dict[str, object]:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }


def _receive_nothing() -> Receive:
    """A client that sends an empty request and then just stays connected."""
    sent = False
    never = asyncio.Event()

    async def receive() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await never.wait()
        return {"type": "http.disconnect"}

    return receive
