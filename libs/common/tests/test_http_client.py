"""Retries, the breaker and the translation of a foreign error."""

from __future__ import annotations

import httpx
import pytest

from barber_common.context import bind_context
from barber_common.http.circuit_breaker import CircuitBreaker, UpstreamUnavailable
from barber_common.http.client import RetryPolicy, ServiceClient, UpstreamError
from barber_common.middleware import CORRELATION_ID_HEADER

FAST_RETRY = RetryPolicy(attempts=3, initial_delay_seconds=0.001, max_delay_seconds=0.002)


class Upstream:
    """A catalog that answers whatever the test tells it to."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self._responses = list(responses)
        self.calls = 0
        self.headers: list[httpx.Headers] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.headers.append(request.headers)
        answer = self._responses[min(self.calls, len(self._responses)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer


def build_client(upstream: Upstream, *, breaker: CircuitBreaker | None = None) -> ServiceClient:
    return ServiceClient(
        base_url="http://catalog",
        upstream="catalog",
        retry=FAST_RETRY,
        breaker=breaker,
        transport=upstream.transport(),
    )


async def test_get_is_retried_on_503_until_it_succeeds() -> None:
    upstream = Upstream(
        httpx.Response(503),
        httpx.Response(503),
        httpx.Response(200, json={"master_id": "m-1"}),
    )

    async with build_client(upstream) as client:
        response = await client.get("/internal/v1/masters/m-1")

    assert response.status_code == 200
    assert upstream.calls == 3


async def test_get_is_retried_on_a_timeout() -> None:
    upstream = Upstream(
        httpx.ReadTimeout("too slow"),
        httpx.Response(200, json={}),
    )

    async with build_client(upstream) as client:
        response = await client.get("/internal/v1/masters/m-1")

    assert response.status_code == 200
    assert upstream.calls == 2


async def test_a_service_that_never_answers_raises_upstream_unavailable() -> None:
    upstream = Upstream(httpx.ConnectError("no route"))

    async with build_client(upstream) as client:
        with pytest.raises(UpstreamUnavailable):
            await client.get("/internal/v1/masters/m-1")

    assert upstream.calls == FAST_RETRY.attempts


async def test_post_is_never_retried() -> None:
    upstream = Upstream(httpx.Response(503), httpx.Response(200))

    async with build_client(upstream) as client:
        with pytest.raises(UpstreamError):
            await client.post("/internal/v1/bookings", json={})

    assert upstream.calls == 1


async def test_foreign_problem_document_becomes_an_exception_with_its_code() -> None:
    upstream = Upstream(
        httpx.Response(
            404,
            headers={"content-type": "application/problem+json"},
            json={"code": "not_found", "detail": "No such master", "status": 404},
        )
    )

    async with build_client(upstream) as client:
        with pytest.raises(UpstreamError) as error:
            await client.get("/internal/v1/masters/m-1")

    assert error.value.status_code == 404
    assert error.value.remote_code == "not_found"
    assert error.value.detail == "No such master"


async def test_correlation_id_travels_with_the_call() -> None:
    upstream = Upstream(httpx.Response(200))

    async with build_client(upstream) as client:
        with bind_context(correlation_id="c-8f2a"):
            await client.get("/internal/v1/masters/m-1")

    assert upstream.headers[0][CORRELATION_ID_HEADER] == "c-8f2a"


async def test_service_token_is_sent_as_a_bearer_token() -> None:
    upstream = Upstream(httpx.Response(200))

    async with build_client(upstream) as client:
        await client.get("/internal/v1/masters/m-1", service_token="m2m-token")

    assert upstream.headers[0]["authorization"] == "Bearer m2m-token"


async def test_breaker_opens_after_the_threshold_and_stops_calling() -> None:
    upstream = Upstream(httpx.ConnectError("no route"))
    breaker = CircuitBreaker(name="catalog", failure_threshold=3, clock=lambda: 0.0)

    async with build_client(upstream, breaker=breaker) as client:
        with pytest.raises(UpstreamUnavailable):
            await client.get("/internal/v1/masters/m-1")
        calls_before = upstream.calls

        with pytest.raises(UpstreamUnavailable):
            await client.get("/internal/v1/masters/m-1")

    assert upstream.calls == calls_before


async def test_breaker_lets_a_call_through_after_the_reset_timeout() -> None:
    upstream = Upstream(
        httpx.ConnectError("no route"),
        httpx.ConnectError("no route"),
        httpx.ConnectError("no route"),
        httpx.Response(200),
    )
    now = 0.0
    breaker = CircuitBreaker(
        name="catalog",
        failure_threshold=3,
        reset_timeout_seconds=30.0,
        clock=lambda: now,
    )

    async with build_client(upstream, breaker=breaker) as client:
        with pytest.raises(UpstreamUnavailable):
            await client.get("/internal/v1/masters/m-1")

        now = 31.0
        response = await client.get("/internal/v1/masters/m-1")

    assert response.status_code == 200
