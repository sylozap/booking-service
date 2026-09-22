"""The catalog client and the cached read over it.

``catalog`` and ``auth`` are replaced by an in-process transport: they are
other services, the edge of this one. Redis is real.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr

from barber_booking.clients.catalog import CatalogClient
from barber_booking.clients.service_token import ServiceTokenProvider
from barber_booking.domain.errors import ServiceNotOffered
from barber_booking.domain.identifiers import MasterId, ServiceId
from barber_booking.services.cache import BookingCache
from barber_booking.services.offerings import ReadOffering
from barber_common.cache import Cache
from barber_common.http import CircuitBreaker, CircuitState, RetryPolicy, ServiceClient
from barber_common.http.circuit_breaker import UpstreamUnavailable
from barber_common.http.client import UpstreamError

pytestmark = pytest.mark.integration

NO_RETRY = RetryPolicy(attempts=1)
NOW = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)


class Clock:
    """Time a test moves by hand."""

    def __init__(self) -> None:
        self.now = NOW
        self.monotonic = 0.0

    def utc(self) -> datetime:
        return self.now

    def seconds(self) -> float:
        return self.monotonic

    def advance(self, delta: timedelta) -> None:
        self.now += delta
        self.monotonic += delta.total_seconds()


class Platform:
    """``auth`` and ``catalog`` as far as this client can see them."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self.offerings: list[httpx.Response | Exception] = []
        self.catalog_calls = 0
        self.token_calls = 0
        self.presented_tokens: list[str | None] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/v1/token":
            self.token_calls += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"token-{self.token_calls}",
                    "token_type": "Bearer",
                    "expires_at": (self._clock.now + timedelta(minutes=5)).isoformat(),
                    "scopes": ["catalog:read"],
                },
            )

        self.catalog_calls += 1
        self.presented_tokens.append(request.headers.get("Authorization"))
        answer = self.offerings.pop(0) if self.offerings else offering_response()
        if isinstance(answer, Exception):
            raise answer
        return answer


def offering_document(*, master_active: bool = True) -> dict[str, object]:
    return {
        "master_id": str(MASTER),
        "salon_id": str(uuid4()),
        "master_active": master_active,
        "service_id": str(SERVICE),
        "service_name": "Haircut",
        "duration_min": 45,
        "price": "3500.00",
        "currency": "RUB",
        "salon": {
            "timezone": "Europe/Moscow",
            "slot_step_min": 15,
            "booking_min_lead_min": 120,
            "booking_horizon_days": 60,
            "cancel_deadline_min": 240,
        },
    }


def offering_response(**overrides: bool) -> httpx.Response:
    return httpx.Response(200, json=offering_document(**overrides))


def problem(status: int, code: str) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps({"status": status, "code": code, "detail": code}),
        headers={"content-type": "application/problem+json"},
    )


MASTER = MasterId(UUID("0192f3c1-0000-7000-8000-000000000001"))
SERVICE = ServiceId(UUID("0192f3c1-0000-7000-8000-000000000002"))


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def platform(clock: Clock) -> Platform:
    return Platform(clock)


@pytest.fixture
def breaker(clock: Clock) -> CircuitBreaker:
    return CircuitBreaker(
        name="catalog", failure_threshold=2, reset_timeout_seconds=30.0, clock=clock.seconds
    )


@pytest.fixture
def catalog(platform: Platform, clock: Clock, breaker: CircuitBreaker) -> CatalogClient:
    transport = platform.transport()
    return CatalogClient(
        http=ServiceClient(
            base_url="http://catalog",
            upstream="catalog",
            retry=NO_RETRY,
            breaker=breaker,
            transport=transport,
        ),
        tokens=ServiceTokenProvider(
            http=ServiceClient(base_url="http://auth", upstream="auth", transport=transport),
            client_id="booking",
            client_secret=SecretStr("secret"),
            clock=clock.utc,
        ),
    )


async def read(catalog: CatalogClient) -> object:
    return await catalog.get_offering(master_id=MASTER, service_id=SERVICE)


async def test_the_offering_arrives_as_the_shared_contract(
    catalog: CatalogClient, platform: Platform
) -> None:
    details = await catalog.get_offering(master_id=MASTER, service_id=SERVICE)

    assert details.master_id == MASTER
    assert details.price == Decimal("3500.00")
    assert details.salon.timezone == "Europe/Moscow"
    assert platform.presented_tokens == ["Bearer token-1"]


async def test_a_missing_link_becomes_service_not_offered(
    catalog: CatalogClient, platform: Platform
) -> None:
    platform.offerings.append(problem(404, "not_found"))

    with pytest.raises(ServiceNotOffered) as failure:
        await read(catalog)

    assert failure.value.code == "service_not_offered"


async def test_an_inactive_master_is_a_flag_rather_than_an_error(
    catalog: CatalogClient, platform: Platform
) -> None:
    platform.offerings.append(offering_response(master_active=False))

    details = await catalog.get_offering(master_id=MASTER, service_id=SERVICE)

    assert details.master_active is False


async def test_a_catalog_that_does_not_answer_is_upstream_unavailable(
    catalog: CatalogClient, platform: Platform
) -> None:
    platform.offerings.append(httpx.ConnectError("connection refused"))

    with pytest.raises(UpstreamUnavailable) as failure:
        await read(catalog)

    assert failure.value.http_status == 503


async def test_the_breaker_opens_and_stops_calling_catalog(
    catalog: CatalogClient, platform: Platform, breaker: CircuitBreaker
) -> None:
    platform.offerings.extend([problem(503, "internal_error"), problem(503, "internal_error")])
    for _ in range(2):
        with pytest.raises(UpstreamError):
            await read(catalog)

    with pytest.raises(UpstreamUnavailable):
        await read(catalog)

    assert breaker.state is CircuitState.OPEN
    assert platform.catalog_calls == 2


async def test_the_breaker_closes_again_once_catalog_answers(
    catalog: CatalogClient, platform: Platform, breaker: CircuitBreaker, clock: Clock
) -> None:
    platform.offerings.extend([problem(503, "internal_error"), problem(503, "internal_error")])
    for _ in range(2):
        with pytest.raises(UpstreamError):
            await read(catalog)

    clock.advance(timedelta(seconds=30))
    details = await catalog.get_offering(master_id=MASTER, service_id=SERVICE)

    assert details.service_id == SERVICE
    assert breaker.state is CircuitState.CLOSED


async def test_one_token_serves_many_calls(catalog: CatalogClient, platform: Platform) -> None:
    for _ in range(3):
        await read(catalog)

    assert platform.token_calls == 1


async def test_a_token_about_to_expire_is_fetched_again(
    catalog: CatalogClient, platform: Platform, clock: Clock
) -> None:
    await read(catalog)

    clock.advance(timedelta(minutes=4, seconds=45))
    await read(catalog)

    assert platform.token_calls == 2
    assert platform.presented_tokens[-1] == "Bearer token-2"


async def test_a_refused_token_is_replaced_and_the_call_repeated_once(
    catalog: CatalogClient, platform: Platform
) -> None:
    platform.offerings.append(problem(401, "unauthorized"))

    details = await catalog.get_offering(master_id=MASTER, service_id=SERVICE)

    assert details.master_id == MASTER
    assert platform.presented_tokens == ["Bearer token-1", "Bearer token-2"]


async def test_a_second_read_is_served_from_the_cache(
    catalog: CatalogClient, platform: Platform, cache: Cache
) -> None:
    scenario = ReadOffering(catalog, BookingCache(cache))

    first = await scenario.execute(master_id=MASTER, service_id=SERVICE)
    second = await scenario.execute(master_id=MASTER, service_id=SERVICE)

    assert second == first
    assert platform.catalog_calls == 1


async def test_a_cache_that_is_down_falls_through_to_catalog(
    catalog: CatalogClient, platform: Platform, unreachable_cache: Cache
) -> None:
    scenario = ReadOffering(catalog, BookingCache(unreachable_cache))

    await scenario.execute(master_id=MASTER, service_id=SERVICE)
    details = await scenario.execute(master_id=MASTER, service_id=SERVICE)

    assert details.master_id == MASTER
    assert platform.catalog_calls == 2
