"""/api/v1/availability through the real ASGI stack."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from barber_booking.models.master_settings import MasterSettings
from barber_common.cache import Cache
from barber_common.testing.fixtures import app_client

pytestmark = pytest.mark.integration


class CatalogStub(Protocol):
    """The part of the fake catalog these tests bend.

    A protocol rather than the class itself: pytest runs this suite in
    importlib mode, so ``tests.conftest`` is not an importable module.
    """

    master_active: bool
    booking_horizon_days: int
    answers: str
    calls: int


MasterSettingsFactory = Callable[..., Awaitable[MasterSettings]]
TemplateFactory = Callable[..., Awaitable[object]]
BookingFactory = Callable[..., Awaitable[object]]

AVAILABILITY = "/api/v1/availability"

# Far enough ahead to clear the minimum lead, well inside the horizon.
WORKDAY = datetime.now(UTC).date() + timedelta(days=30)
SERVICE_ID = uuid4()


@pytest.fixture
def app_with_cache(app: FastAPI, cache: Cache) -> Iterator[FastAPI]:
    app.state.cache = cache

    yield app

    app.state.cache = None


async def ask(
    application: FastAPI,
    *,
    master_id: UUID,
    service_id: UUID | None = SERVICE_ID,
    date_from: date | None = WORKDAY,
    date_to: date | None = None,
) -> httpx.Response:
    params: dict[str, str] = {"master_id": str(master_id)}
    if service_id is not None:
        params["service_id"] = str(service_id)
    if date_from is not None:
        params["date_from"] = date_from.isoformat()
    if date_to is not None:
        params["date_to"] = date_to.isoformat()

    async with app_client(application) as client:
        return await client.get(AVAILABILITY, params=params)


async def working_master(
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
    *,
    timezone: str = "Europe/Moscow",
) -> MasterSettings:
    """A master who works ten to eight on the day under test."""
    master = await make_master_settings(timezone=timezone)
    await make_template(
        master_id=master.master_id,
        weekday=WORKDAY.weekday(),
        start_time=time(10),
        end_time=time(20),
    )
    return master


async def test_a_visitor_without_a_token_sees_the_free_starts(
    app: FastAPI,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await working_master(make_master_settings, make_template)

    answer = await ask(app, master_id=master.master_id)

    assert answer.status_code == 200
    body = answer.json()
    assert body["master_active"] is True
    assert body["duration_min"] == 45
    assert body["timezone"] == "Europe/Moscow"
    [day] = body["days"]
    assert day["date"] == WORKDAY.isoformat()
    assert day["slots"][0].startswith(WORKDAY.isoformat())


async def test_a_missing_service_id_is_422(
    app: FastAPI,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await working_master(make_master_settings, make_template)

    answer = await ask(app, master_id=master.master_id, service_id=None)

    assert answer.status_code == 422
    assert answer.json()["code"] == "validation_error"


async def test_a_window_wider_than_two_weeks_is_422(
    app: FastAPI,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await working_master(make_master_settings, make_template)

    answer = await ask(app, master_id=master.master_id, date_to=WORKDAY + timedelta(days=14))

    assert answer.status_code == 422


async def test_a_window_that_ends_before_it_starts_is_422(
    app: FastAPI,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await working_master(make_master_settings, make_template)

    answer = await ask(app, master_id=master.master_id, date_to=WORKDAY - timedelta(days=1))

    assert answer.status_code == 422


async def test_every_date_of_the_window_is_answered(
    app: FastAPI,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await working_master(make_master_settings, make_template)

    answer = await ask(app, master_id=master.master_id, date_to=WORKDAY + timedelta(days=3))

    days = answer.json()["days"]
    assert [day["date"] for day in days] == [
        (WORKDAY + timedelta(days=offset)).isoformat() for offset in range(4)
    ]
    # Only one weekday is in the template, so the rest are working days with
    # nothing free rather than missing entries.
    assert sum(1 for day in days if day["slots"]) == 1


async def test_a_deactivated_master_answers_with_a_flag_and_no_slots(
    app: FastAPI,
    catalog: CatalogStub,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await working_master(make_master_settings, make_template)
    catalog.master_active = False

    answer = await ask(app, master_id=master.master_id)

    assert answer.status_code == 200
    assert answer.json()["master_active"] is False
    assert answer.json()["days"][0]["slots"] == []


async def test_a_master_without_a_schedule_answers_with_empty_days(
    app: FastAPI, make_master_settings: MasterSettingsFactory
) -> None:
    master = await make_master_settings()

    answer = await ask(app, master_id=master.master_id)

    assert answer.status_code == 200
    assert answer.json()["days"][0]["slots"] == []


async def test_a_master_booking_knows_nothing_about_answers_with_empty_days(
    app: FastAPI,
) -> None:
    answer = await ask(app, master_id=uuid4())

    assert answer.status_code == 200
    assert answer.json()["days"][0]["slots"] == []


async def test_dates_beyond_the_horizon_come_back_empty(
    app: FastAPI,
    catalog: CatalogStub,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await working_master(make_master_settings, make_template)
    catalog.booking_horizon_days = 7

    answer = await ask(app, master_id=master.master_id)

    assert answer.json()["days"][0]["slots"] == []


async def test_a_service_the_master_does_not_offer_is_422(
    app: FastAPI,
    catalog: CatalogStub,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await working_master(make_master_settings, make_template)
    catalog.answers = "not_offered"

    answer = await ask(app, master_id=master.master_id)

    assert answer.status_code == 422
    assert answer.json()["code"] == "service_not_offered"


async def test_a_catalog_that_is_down_is_503(
    app: FastAPI,
    catalog: CatalogStub,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await working_master(make_master_settings, make_template)
    catalog.answers = "gone"

    answer = await ask(app, master_id=master.master_id)

    assert answer.status_code == 503
    assert answer.json()["code"] == "upstream_unavailable"


async def test_a_second_request_is_served_from_the_cache(
    app_with_cache: FastAPI,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
    make_booking: BookingFactory,
) -> None:
    master = await working_master(make_master_settings, make_template)
    first = await ask(app_with_cache, master_id=master.master_id)
    taken = datetime.combine(WORKDAY, time(7), tzinfo=UTC)

    await make_booking(master_id=master.master_id, start_at=taken, duration_min=45)
    again = await ask(app_with_cache, master_id=master.master_id)

    # The booking is real, but the cached answer is a minute old and still
    # offers the slot: availability is an estimate, and the constraint is what
    # refuses the second booking.
    assert again.json() == first.json()
