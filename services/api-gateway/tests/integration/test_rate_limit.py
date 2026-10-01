"""Rate limits counted in a real Redis, through the real gateway.

The clock is the test's: the window slides when the test moves it, not when a
minute of wall time has passed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from barber_common.cache import cache_from_dsn
from barber_common.errors import PROBLEM_CONTENT_TYPE
from barber_common.testing.fixtures import app_client
from barber_gateway.rate_limit import SlidingWindowLimiter

pytestmark = pytest.mark.integration

BearerFactory = Callable[..., dict[str, str]]

# Settings of the suite are the defaults of docs/04.
ANONYMOUS_PER_MINUTE = 30
USER_PER_MINUTE = 120
BOOKINGS_PER_MINUTE = 5
REGISTRATIONS_PER_HOUR = 3

# The start of a minute, so a test knows where the window boundary is.
START = 1_790_000_040.0


class Clock:
    """Wall time as far as the limiter knows, moved by hand."""

    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock(START)


@pytest.fixture
async def limited_app(app: FastAPI, redis_dsn: str, clock: Clock) -> AsyncIterator[FastAPI]:
    """The gateway with a limiter over the Redis of the session.

    A key prefix of its own per test, so no test sees what another counted and
    nothing has to be flushed.
    """
    limiter = SlidingWindowLimiter(
        cache_from_dsn(redis_dsn, timeout_seconds=1.0),
        clock=clock,
        key_prefix=f"test-{uuid4().hex}",
    )
    app.state.rate_limiter = limiter

    yield app

    await limiter.aclose()


@pytest.fixture
async def http(limited_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with app_client(limited_app) as client:
        yield client


async def _spend(
    http: httpx.AsyncClient,
    times: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
) -> list[int]:
    """Send the same request a number of times; the statuses, in order."""
    statuses = []
    for _ in range(times):
        response = await http.request(method, path, headers=headers)
        statuses.append(response.status_code)
    return statuses


async def test_a_stranger_over_the_limit_is_told_to_come_back_later(
    http: httpx.AsyncClient,
) -> None:
    allowed = await _spend(http, ANONYMOUS_PER_MINUTE, "GET", "/api/v1/salons")

    response = await http.get("/api/v1/salons")

    assert set(allowed) == {200}
    assert response.status_code == 429
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    assert response.json()["code"] == "rate_limited"
    assert int(response.headers["retry-after"]) >= 1


async def test_a_signed_in_user_has_a_quota_of_their_own(
    http: httpx.AsyncClient, bearer: BearerFactory
) -> None:
    await _spend(http, ANONYMOUS_PER_MINUTE, "GET", "/api/v1/salons")
    user = bearer()

    statuses = await _spend(http, USER_PER_MINUTE, "GET", "/api/v1/salons", headers=user)
    over = await http.get("/api/v1/salons", headers=user)

    # The strangers from the same address have spent theirs; the user has not.
    assert set(statuses) == {200}
    assert over.status_code == 429


async def test_two_users_do_not_share_a_quota(
    http: httpx.AsyncClient, bearer: BearerFactory
) -> None:
    first, second = bearer(), bearer()
    await _spend(http, USER_PER_MINUTE, "GET", "/api/v1/bookings", headers=first)

    refused = await http.get("/api/v1/bookings", headers=first)
    admitted = await http.get("/api/v1/bookings", headers=second)

    assert refused.status_code == 429
    assert admitted.status_code == 200


async def test_creating_bookings_is_limited_harder_than_anything_else(
    http: httpx.AsyncClient, bearer: BearerFactory
) -> None:
    user = bearer()
    created = await _spend(http, BOOKINGS_PER_MINUTE, "POST", "/api/v1/bookings", headers=user)

    another = await http.post("/api/v1/bookings", headers=user)
    a_read = await http.get("/api/v1/bookings", headers=user)

    assert BOOKINGS_PER_MINUTE < USER_PER_MINUTE
    assert set(created) == {200}
    assert another.status_code == 429
    assert another.json()["scope"] == "booking_create"
    assert a_read.status_code == 200


async def test_registering_is_limited_per_hour_by_address(
    http: httpx.AsyncClient, clock: Clock
) -> None:
    await _spend(http, REGISTRATIONS_PER_HOUR, "POST", "/api/v1/auth/register")
    clock.now += 30 * 60

    refused = await http.post("/api/v1/auth/register")
    a_login = await http.post("/api/v1/auth/login")

    assert refused.status_code == 429
    assert refused.json()["scope"] == "registration"
    assert a_login.status_code == 200


async def test_the_window_slides_rather_than_resetting_on_the_minute(
    http: httpx.AsyncClient, clock: Clock
) -> None:
    clock.now = START + 59
    await _spend(http, ANONYMOUS_PER_MINUTE, "GET", "/api/v1/salons")

    clock.now = START + 61
    just_after_the_boundary = await http.get("/api/v1/salons")
    clock.now = START + 60 + 58
    a_minute_later = await http.get("/api/v1/salons")

    # A fixed window would have started counting from zero at START + 60.
    assert just_after_the_boundary.status_code == 429
    assert a_minute_later.status_code == 200


async def test_a_bad_token_is_counted_before_it_is_refused(
    http: httpx.AsyncClient,
) -> None:
    forged = {"Authorization": "Bearer forged"}
    refusals = await _spend(http, ANONYMOUS_PER_MINUTE, "GET", "/api/v1/bookings", headers=forged)

    response = await http.get("/api/v1/bookings", headers=forged)

    # Guessing tokens is limited like anything else a stranger does.
    assert set(refusals) == {401}
    assert response.status_code == 429


async def test_a_forged_forwarded_address_buys_no_fresh_quota(
    http: httpx.AsyncClient,
) -> None:
    for number in range(ANONYMOUS_PER_MINUTE):
        await http.get("/api/v1/salons", headers={"X-Forwarded-For": f"203.0.113.{number}"})

    response = await http.get("/api/v1/salons", headers={"X-Forwarded-For": "198.51.100.1"})

    assert response.status_code == 429


async def test_requests_go_through_when_redis_is_down(app: FastAPI) -> None:
    # Nothing listens on port 1: every call to Redis fails at once.
    limiter = SlidingWindowLimiter(cache_from_dsn("redis://127.0.0.1:1/0", timeout_seconds=0.2))
    app.state.rate_limiter = limiter

    async with app_client(app) as http:
        statuses = await _spend(http, ANONYMOUS_PER_MINUTE + 5, "GET", "/api/v1/salons")

    await limiter.aclose()
    assert set(statuses) == {200}
