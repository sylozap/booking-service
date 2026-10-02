"""The card of a master: catalog and booking in one answer."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Protocol
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest

from barber_common.contracts.booking import AvailabilityResponse, DayAvailability
from barber_common.contracts.catalog import MasterCardResponse, OfferedServiceResponse
from barber_common.errors import PROBLEM_CONTENT_TYPE
from barber_gateway.routing import Upstream

BearerFactory = Callable[..., dict[str, str]]
Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]

MASTER = uuid4()
SERVICE = uuid4()
SALON = uuid4()


class FakeServices(Protocol):
    """The fake services of the conftest, as far as these tests use them.

    A protocol rather than the class itself: pytest runs this suite in
    importlib mode, so ``tests.conftest`` is not an importable module.
    """

    def answer(self, upstream: Upstream, handler: Handler) -> None: ...

    def reached(self, upstream: Upstream) -> list[httpx.Request]: ...


def profile() -> MasterCardResponse:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    return MasterCardResponse(
        id=MASTER,
        salon_id=SALON,
        user_id=uuid4(),
        display_name="Arkady",
        bio=None,
        photo_url=None,
        specialization="fades",
        is_active=True,
        created_at=now,
        updated_at=now,
        services=[
            OfferedServiceResponse(
                service_id=SERVICE,
                name="Haircut",
                description=None,
                price=Decimal("3500.00"),
                currency="RUB",
                duration_min=45,
                base_price=Decimal("3000.00"),
                base_duration_min=45,
            )
        ],
    )


def availability(starts_per_day: int = 8, days: int = 2) -> AvailabilityResponse:
    first = datetime.now(UTC).replace(hour=6, minute=0, second=0, microsecond=0)
    return AvailabilityResponse(
        master_id=MASTER,
        service_id=SERVICE,
        timezone="Europe/Moscow",
        master_active=True,
        duration_min=45,
        days=[
            DayAvailability(
                date=(first + timedelta(days=day)).date(),
                slots=[
                    first + timedelta(days=day, minutes=45 * slot) for slot in range(starts_per_day)
                ],
            )
            for day in range(days)
        ],
    )


def answering(body: str, *, status: int = 200, delay: float = 0.0) -> Handler:
    async def handler(request: httpx.Request) -> httpx.Response:
        if delay:
            await asyncio.sleep(delay)
        return httpx.Response(
            status, content=body.encode(), headers={"Content-Type": "application/json"}
        )

    return handler


def failing(error: Exception) -> Handler:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise error

    return handler


def problem(status: int, code: str) -> Handler:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            json={"status": status, "code": code, "detail": code},
            headers={"Content-Type": PROBLEM_CONTENT_TYPE},
        )

    return handler


@pytest.fixture
def card_of_a_working_master(services: FakeServices) -> AvailabilityResponse:
    slots = availability()
    services.answer(Upstream.CATALOG, answering(profile().model_dump_json()))
    services.answer(Upstream.BOOKING, answering(slots.model_dump_json()))
    return slots


async def test_the_card_glues_the_profile_and_the_nearest_slots(
    client: httpx.AsyncClient, card_of_a_working_master: AvailabilityResponse
) -> None:
    response = await client.get(f"/api/v1/masters/{MASTER}/card?service_id={SERVICE}")

    card = response.json()
    expected_slots = [start for day in card_of_a_working_master.days for start in day.slots][:10]
    assert response.status_code == 200
    assert card["master"]["display_name"] == "Arkady"
    assert card["master"]["services"][0]["price"] == "3500.00"
    assert card["service_id"] == str(SERVICE)
    assert card["timezone"] == "Europe/Moscow"
    assert card["slots_status"] == "ok"
    assert [datetime.fromisoformat(start) for start in card["slots"]] == expected_slots


async def test_the_slots_are_read_for_a_week_from_yesterday(
    client: httpx.AsyncClient,
    services: FakeServices,
    card_of_a_working_master: AvailabilityResponse,
) -> None:
    await client.get(f"/api/v1/masters/{MASTER}/card?service_id={SERVICE}")

    asked = parse_qs(services.reached(Upstream.BOOKING)[0].url.query.decode())
    yesterday = datetime.now(UTC).date() - timedelta(days=1)
    assert asked["master_id"] == [str(MASTER)]
    assert asked["service_id"] == [str(SERVICE)]
    assert date.fromisoformat(asked["date_from"][0]) == yesterday
    assert date.fromisoformat(asked["date_to"][0]) == yesterday + timedelta(days=7)


async def test_catalog_and_booking_are_asked_at_the_same_time(
    client: httpx.AsyncClient, services: FakeServices
) -> None:
    delay = 0.3
    services.answer(Upstream.CATALOG, answering(profile().model_dump_json(), delay=delay))
    services.answer(Upstream.BOOKING, answering(availability().model_dump_json(), delay=delay))

    started = time.perf_counter()
    response = await client.get(f"/api/v1/masters/{MASTER}/card?service_id={SERVICE}")
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert elapsed < 2 * delay


@pytest.mark.parametrize(
    "booking",
    [
        failing(httpx.ConnectError("connection refused")),
        failing(httpx.ReadTimeout("read timed out")),
        problem(500, "internal_error"),
    ],
)
async def test_without_booking_the_card_is_served_without_slots(
    client: httpx.AsyncClient, services: FakeServices, booking: Handler
) -> None:
    services.answer(Upstream.CATALOG, answering(profile().model_dump_json()))
    services.answer(Upstream.BOOKING, booking)

    response = await client.get(f"/api/v1/masters/{MASTER}/card?service_id={SERVICE}")

    card = response.json()
    assert response.status_code == 200
    assert card["master"]["id"] == str(MASTER)
    assert card["slots"] == []
    assert card["slots_status"] == "unavailable"
    assert card["timezone"] is None


async def test_a_master_with_nothing_free_is_not_mistaken_for_an_outage(
    client: httpx.AsyncClient, services: FakeServices
) -> None:
    services.answer(Upstream.CATALOG, answering(profile().model_dump_json()))
    services.answer(Upstream.BOOKING, answering(availability(starts_per_day=0).model_dump_json()))

    response = await client.get(f"/api/v1/masters/{MASTER}/card?service_id={SERVICE}")

    assert response.json()["slots"] == []
    assert response.json()["slots_status"] == "ok"


async def test_without_catalog_there_is_no_card(
    client: httpx.AsyncClient, services: FakeServices
) -> None:
    services.answer(Upstream.CATALOG, failing(httpx.ConnectError("connection refused")))
    services.answer(Upstream.BOOKING, answering(availability().model_dump_json()))

    response = await client.get(f"/api/v1/masters/{MASTER}/card?service_id={SERVICE}")

    assert response.status_code == 503
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    assert response.json()["code"] == "upstream_unavailable"


async def test_a_master_catalog_does_not_know_is_a_404(
    client: httpx.AsyncClient, services: FakeServices
) -> None:
    services.answer(Upstream.CATALOG, problem(404, "not_found"))
    services.answer(Upstream.BOOKING, problem(404, "not_found"))

    response = await client.get(f"/api/v1/masters/{MASTER}/card?service_id={SERVICE}")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


async def test_a_service_the_master_does_not_offer_is_the_callers_mistake(
    client: httpx.AsyncClient, services: FakeServices
) -> None:
    services.answer(Upstream.CATALOG, answering(profile().model_dump_json()))
    services.answer(Upstream.BOOKING, problem(422, "service_not_offered"))

    response = await client.get(f"/api/v1/masters/{MASTER}/card?service_id={uuid4()}")

    assert response.status_code == 422
    assert response.json()["code"] == "service_not_offered"


async def test_without_a_service_no_slots_are_asked_for(
    client: httpx.AsyncClient, services: FakeServices
) -> None:
    services.answer(Upstream.CATALOG, answering(profile().model_dump_json()))

    response = await client.get(f"/api/v1/masters/{MASTER}/card")

    assert response.status_code == 200
    assert response.json()["slots_status"] == "not_requested"
    assert response.json()["service_id"] is None
    assert services.reached(Upstream.BOOKING) == []


@pytest.mark.parametrize(
    "path", ["/api/v1/masters/not-a-uuid/card", f"/api/v1/masters/{MASTER}/card?service_id=x"]
)
async def test_a_malformed_id_is_refused_before_anything_is_asked(
    client: httpx.AsyncClient, services: FakeServices, path: str
) -> None:
    response = await client.get(path)

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert services.reached(Upstream.CATALOG) == []


async def test_an_invalid_token_is_refused_on_the_card_too(
    client: httpx.AsyncClient, services: FakeServices, bearer: BearerFactory
) -> None:
    an_hour_ago = datetime.now(UTC) - timedelta(hours=1)

    response = await client.get(
        f"/api/v1/masters/{MASTER}/card", headers=bearer(iat=an_hour_ago, exp=an_hour_ago)
    )

    assert response.status_code == 401
    assert services.reached(Upstream.CATALOG) == []


async def test_the_card_is_open_to_a_signed_in_user_too(
    user_client: httpx.AsyncClient, card_of_a_working_master: AvailabilityResponse
) -> None:
    response = await user_client.get(f"/api/v1/masters/{MASTER}/card?service_id={SERVICE}")

    assert response.status_code == 200
