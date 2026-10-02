"""The domain metrics of booking, read where Prometheus reads them."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, time, timedelta
from typing import Protocol
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from barber_booking.models.booking import Booking
from barber_booking.models.master_settings import MasterSettings
from barber_common.cache import Cache
from barber_common.testing.fixtures import app_client

pytestmark = pytest.mark.integration


class CatalogStub(Protocol):
    """The part of the fake catalog these tests read."""

    salon_id: UUID


MasterSettingsFactory = Callable[..., Awaitable[MasterSettings]]
TemplateFactory = Callable[..., Awaitable[object]]
BookingFactory = Callable[..., Awaitable[Booking]]
AuthorizationFactory = Callable[..., dict[str, str]]

BOOKINGS = "/api/v1/bookings"
AVAILABILITY = "/api/v1/availability"

WORKDAY = datetime.now(UTC).date() + timedelta(days=30)
TEN_MOSCOW = datetime.combine(WORKDAY, time(7), tzinfo=UTC)
SERVICE_ID = uuid4()


@pytest.fixture
def app_with_cache(app: FastAPI, cache: Cache) -> Iterator[FastAPI]:
    app.state.cache = cache

    yield app

    app.state.cache = None


async def booked_master(
    make_master_settings: MasterSettingsFactory, make_template: TemplateFactory
) -> MasterSettings:
    master = await make_master_settings()
    await make_template(
        master_id=master.master_id,
        weekday=WORKDAY.weekday(),
        start_time=time(10),
        end_time=time(20),
    )
    return master


async def book(
    app: FastAPI, master: MasterSettings, start_at: datetime, headers: dict[str, str]
) -> httpx.Response:
    async with app_client(app) as client:
        return await client.post(
            BOOKINGS,
            json={
                "master_id": str(master.master_id),
                "service_id": str(SERVICE_ID),
                "start_at": start_at.isoformat(),
            },
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )


async def metrics(app: FastAPI) -> str:
    async with app_client(app) as client:
        response = await client.get("/metrics")
    return response.text


def sample(text: str, name: str, **labels: str) -> float:
    """The value of one sample, or zero when it was never touched."""
    wanted = ",".join(f'{key}="{value}"' for key, value in labels.items())
    prefix = f"{name}{{{wanted}}}" if wanted else name
    for line in text.splitlines():
        if line.startswith(prefix):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


async def test_a_created_booking_moves_the_counter_of_its_salon(
    app: FastAPI,
    catalog: CatalogStub,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await booked_master(make_master_settings, make_template)
    salon_id = str(catalog.salon_id)
    before = sample(
        await metrics(app), "bookings_created_total", salon=salon_id, status="confirmed"
    )

    await book(app, master, TEN_MOSCOW, authorize())

    after = sample(await metrics(app), "bookings_created_total", salon=salon_id, status="confirmed")
    assert after == before + 1


async def test_a_lost_race_moves_the_conflict_counter(
    app: FastAPI,
    catalog: CatalogStub,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await booked_master(make_master_settings, make_template)
    salon_id = str(catalog.salon_id)
    await book(app, master, TEN_MOSCOW, authorize())
    before = sample(await metrics(app), "booking_conflicts_total", salon=salon_id)

    await book(app, master, TEN_MOSCOW, authorize())

    after = sample(await metrics(app), "booking_conflicts_total", salon=salon_id)
    assert after == before + 1


async def post(app: FastAPI, path: str, headers: dict[str, str]) -> httpx.Response:
    async with app_client(app) as client:
        return await client.post(path, headers=headers)


def closed(text: str, salon_id: UUID, status: str) -> float:
    return sample(text, "bookings_closed_total", salon=str(salon_id), status=status)


async def test_a_cancellation_by_the_client_is_counted_as_one(
    app: FastAPI, authorize: AuthorizationFactory, make_booking: BookingFactory
) -> None:
    client_id, salon_id = uuid4(), uuid4()
    booking = await make_booking(
        master_id=uuid4(),
        salon_id=salon_id,
        client_user_id=client_id,
        start_at=datetime.now(UTC) + timedelta(days=2),
    )

    await post(app, f"{BOOKINGS}/{booking.id}/cancel", authorize(user_id=client_id))

    assert closed(await metrics(app), salon_id, "cancelled_by_client") == 1


async def test_a_cancellation_by_the_salon_is_counted_apart(
    app: FastAPI, authorize: AuthorizationFactory, make_booking: BookingFactory
) -> None:
    salon_id = uuid4()
    booking = await make_booking(
        master_id=uuid4(), salon_id=salon_id, start_at=datetime.now(UTC) + timedelta(days=2)
    )

    await post(
        app, f"{BOOKINGS}/{booking.id}/cancel", authorize(roles=(("salon_admin", salon_id),))
    )

    published = await metrics(app)
    assert closed(published, salon_id, "cancelled_by_salon") == 1
    assert closed(published, salon_id, "cancelled_by_client") == 0


async def test_a_refused_cancellation_is_not_counted(
    app: FastAPI, authorize: AuthorizationFactory, make_booking: BookingFactory
) -> None:
    client_id, salon_id = uuid4(), uuid4()
    booking = await make_booking(
        master_id=uuid4(),
        salon_id=salon_id,
        client_user_id=client_id,
        start_at=datetime.now(UTC) + timedelta(hours=1),
        cancel_deadline_min=240,
    )

    response = await post(app, f"{BOOKINGS}/{booking.id}/cancel", authorize(user_id=client_id))

    assert response.json()["code"] == "cancel_deadline_passed"
    assert closed(await metrics(app), salon_id, "cancelled_by_client") == 0


@pytest.mark.parametrize(("action", "status"), [("complete", "completed"), ("no-show", "no_show")])
async def test_a_closed_visit_is_counted_by_its_outcome(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_booking: BookingFactory,
    action: str,
    status: str,
) -> None:
    master = await make_master_settings(salon_id=uuid4())
    booking = await make_booking(
        master_id=master.master_id,
        salon_id=master.salon_id,
        start_at=datetime.now(UTC) - timedelta(minutes=20),
    )
    admin = authorize(roles=(("salon_admin", master.salon_id),))

    await post(app, f"{BOOKINGS}/{booking.id}/{action}", admin)
    # The second call answers with the booking as it is and changes nothing.
    await post(app, f"{BOOKINGS}/{booking.id}/{action}", admin)

    assert closed(await metrics(app), master.salon_id, status) == 1


async def test_an_availability_request_fills_the_histogram(
    app_with_cache: FastAPI,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await booked_master(make_master_settings, make_template)
    counted = "availability_query_duration_seconds_count"
    before = sample(await metrics(app_with_cache), counted, source="database")

    async with app_client(app_with_cache) as client:
        await client.get(
            AVAILABILITY,
            params={
                "master_id": str(master.master_id),
                "service_id": str(SERVICE_ID),
                "date_from": WORKDAY.isoformat(),
            },
        )

    after = sample(await metrics(app_with_cache), counted, source="database")
    assert after == before + 1


async def test_a_cached_answer_is_measured_apart_from_a_queried_one(
    app_with_cache: FastAPI,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await booked_master(make_master_settings, make_template)
    counted = "availability_query_duration_seconds_count"
    params = {
        "master_id": str(master.master_id),
        "service_id": str(SERVICE_ID),
        "date_from": WORKDAY.isoformat(),
    }
    before = sample(await metrics(app_with_cache), counted, source="cache")

    async with app_client(app_with_cache) as client:
        await client.get(AVAILABILITY, params=params)
        await client.get(AVAILABILITY, params=params)

    after = sample(await metrics(app_with_cache), counted, source="cache")
    assert after == before + 1


async def test_the_domain_metrics_are_published(app: FastAPI) -> None:
    published = await metrics(app)

    assert "bookings_created_total" in published
    assert "booking_conflicts_total" in published
    assert "availability_query_duration_seconds" in published
    assert "bookings_closed_total" in published
    assert "bookings_upcoming_seconds" in published
