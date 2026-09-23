"""GET /api/v1/bookings and GET /api/v1/bookings/{id}: who sees what."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from barber_booking.models.booking import Booking
from barber_booking.models.master_settings import MasterSettings
from barber_common.testing.fixtures import app_client

pytestmark = pytest.mark.integration

MasterSettingsFactory = Callable[..., Awaitable[MasterSettings]]
BookingFactory = Callable[..., Awaitable[Booking]]
AuthorizationFactory = Callable[..., dict[str, str]]

BOOKINGS = "/api/v1/bookings"
MORNING = datetime(2030, 3, 4, 7, 0, tzinfo=UTC)


def at(hours: int) -> datetime:
    return MORNING + timedelta(hours=hours)


async def listing(
    app: FastAPI, headers: dict[str, str], params: Mapping[str, str | list[str]] | None = None
) -> httpx.Response:
    async with app_client(app) as client:
        return await client.get(BOOKINGS, params=params, headers=headers)


async def listed_ids(
    app: FastAPI, headers: dict[str, str], params: Mapping[str, str | list[str]] | None = None
) -> set[UUID]:
    response = await listing(app, headers, params)
    assert response.status_code == 200, response.text
    return {UUID(item["id"]) for item in response.json()["items"]}


async def read(app: FastAPI, booking_id: UUID, headers: dict[str, str]) -> httpx.Response:
    async with app_client(app) as client:
        return await client.get(f"{BOOKINGS}/{booking_id}", headers=headers)


@dataclass(frozen=True)
class Salons:
    """Two salons, a master in the first, and bookings spread between them.

    ``master_as_client`` is a booking the master made for themselves with
    somebody else -- the case of a master who is also a client.
    """

    salon: UUID
    other_salon: UUID
    master: MasterSettings
    client: UUID
    with_master: Booking
    with_master_by_client: Booking
    with_colleague_by_client: Booking
    master_as_client: Booking
    in_other_salon: Booking


@pytest.fixture
async def salons(
    make_master_settings: MasterSettingsFactory, make_booking: BookingFactory
) -> Salons:
    salon, other_salon, client = uuid4(), uuid4(), uuid4()
    master = await make_master_settings(salon_id=salon)
    colleague = await make_master_settings(salon_id=salon)
    elsewhere = await make_master_settings(salon_id=other_salon)
    return Salons(
        salon=salon,
        other_salon=other_salon,
        master=master,
        client=client,
        with_master=await make_booking(master_id=master.master_id, salon_id=salon, start_at=at(0)),
        with_master_by_client=await make_booking(
            master_id=master.master_id, salon_id=salon, start_at=at(1), client_user_id=client
        ),
        with_colleague_by_client=await make_booking(
            master_id=colleague.master_id, salon_id=salon, start_at=at(2), client_user_id=client
        ),
        master_as_client=await make_booking(
            master_id=elsewhere.master_id,
            salon_id=other_salon,
            start_at=at(3),
            client_user_id=master.user_id,
        ),
        in_other_salon=await make_booking(
            master_id=elsewhere.master_id, salon_id=other_salon, start_at=at(4)
        ),
    )


def as_master(authorize: AuthorizationFactory, salons: Salons) -> dict[str, str]:
    return authorize(
        roles=(("client", None), ("master", salons.salon)), user_id=salons.master.user_id
    )


# --- the matrix of roles ----------------------------------------------------


async def test_a_client_sees_only_their_own_bookings(
    app: FastAPI, authorize: AuthorizationFactory, salons: Salons
) -> None:
    seen = await listed_ids(app, authorize(user_id=salons.client))

    assert seen == {salons.with_master_by_client.id, salons.with_colleague_by_client.id}


async def test_a_master_sees_their_bookings_as_master_and_as_client(
    app: FastAPI, authorize: AuthorizationFactory, salons: Salons
) -> None:
    seen = await listed_ids(app, as_master(authorize, salons))

    assert seen == {
        salons.with_master.id,
        salons.with_master_by_client.id,
        salons.master_as_client.id,
    }


async def test_a_master_without_the_role_sees_only_what_they_booked(
    app: FastAPI, authorize: AuthorizationFactory, salons: Salons
) -> None:
    seen = await listed_ids(app, authorize(user_id=salons.master.user_id))

    assert seen == {salons.master_as_client.id}


async def test_a_salon_admin_sees_the_whole_salon_and_nothing_of_another(
    app: FastAPI, authorize: AuthorizationFactory, salons: Salons
) -> None:
    seen = await listed_ids(app, authorize(roles=(("salon_admin", salons.salon),)))

    assert seen == {
        salons.with_master.id,
        salons.with_master_by_client.id,
        salons.with_colleague_by_client.id,
    }


async def test_a_super_admin_sees_everything(
    app: FastAPI, authorize: AuthorizationFactory, salons: Salons
) -> None:
    seen = await listed_ids(app, authorize(roles=(("super_admin", None),)))

    assert {
        salons.with_master.id,
        salons.with_master_by_client.id,
        salons.with_colleague_by_client.id,
        salons.master_as_client.id,
        salons.in_other_salon.id,
    } <= seen


# --- filters ----------------------------------------------------------------


async def test_the_period_includes_its_start_and_excludes_its_end(
    app: FastAPI, authorize: AuthorizationFactory, salons: Salons
) -> None:
    seen = await listed_ids(
        app,
        authorize(roles=(("salon_admin", salons.salon),)),
        {"from": at(1).isoformat(), "to": at(2).isoformat()},
    )

    assert seen == {salons.with_master_by_client.id}


async def test_the_listing_narrows_to_one_master(
    app: FastAPI, authorize: AuthorizationFactory, salons: Salons
) -> None:
    seen = await listed_ids(
        app,
        authorize(roles=(("salon_admin", salons.salon),)),
        {"master_id": str(salons.master.master_id)},
    )

    assert seen == {salons.with_master.id, salons.with_master_by_client.id}


async def test_a_filter_on_another_salon_finds_nothing_rather_than_failing(
    app: FastAPI, authorize: AuthorizationFactory, salons: Salons
) -> None:
    seen = await listed_ids(
        app,
        authorize(roles=(("salon_admin", salons.salon),)),
        {"salon_id": str(salons.other_salon)},
    )

    assert seen == set()


async def test_several_statuses_are_accepted_at_once(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_booking: BookingFactory,
) -> None:
    client = uuid4()
    cancelled = await make_booking(
        master_id=uuid4(), start_at=at(0), client_user_id=client, status="cancelled_by_client"
    )
    completed = await make_booking(
        master_id=uuid4(), start_at=at(1), client_user_id=client, status="completed"
    )
    await make_booking(master_id=uuid4(), start_at=at(2), client_user_id=client)

    seen = await listed_ids(
        app, authorize(user_id=client), {"status": ["cancelled_by_client", "completed"]}
    )

    assert seen == {cancelled.id, completed.id}


# --- pages ------------------------------------------------------------------


async def test_pages_follow_the_start_and_end_with_a_null_cursor(
    app: FastAPI, authorize: AuthorizationFactory, make_booking: BookingFactory
) -> None:
    client = uuid4()
    created = [
        (await make_booking(master_id=uuid4(), start_at=at(hour), client_user_id=client)).id
        for hour in (4, 1, 3, 0, 2)
    ]
    headers = authorize(user_id=client)

    first = (await listing(app, headers, {"limit": "2"})).json()
    second = (await listing(app, headers, {"limit": "2", "cursor": first["next_cursor"]})).json()
    third = (await listing(app, headers, {"limit": "2", "cursor": second["next_cursor"]})).json()

    pages = [first, second, third]
    starts = [item["start_at"] for page in pages for item in page["items"]]
    assert [len(page["items"]) for page in pages] == [2, 2, 1]
    assert starts == sorted(starts)
    assert {UUID(item["id"]) for page in pages for item in page["items"]} == set(created)
    assert third["next_cursor"] is None


async def test_a_cursor_not_handed_out_by_the_service_is_refused(
    app: FastAPI, authorize: AuthorizationFactory
) -> None:
    response = await listing(app, authorize(), {"cursor": "not-a-cursor"})

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


@pytest.mark.parametrize(
    "params",
    [
        {"limit": "0"},
        {"limit": "101"},
        {"status": "lost"},
        {"from": "2030-03-04T10:00:00"},
        {"from": at(2).isoformat(), "to": at(1).isoformat()},
    ],
)
async def test_an_invalid_query_is_refused(
    app: FastAPI, authorize: AuthorizationFactory, params: dict[str, str]
) -> None:
    response = await listing(app, authorize(), params)

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_a_caller_without_a_token_is_refused(app: FastAPI) -> None:
    response = await listing(app, {})

    assert response.status_code == 401


# --- one booking ------------------------------------------------------------


async def test_the_client_reads_their_booking(
    app: FastAPI, authorize: AuthorizationFactory, salons: Salons
) -> None:
    response = await read(app, salons.with_master_by_client.id, authorize(user_id=salons.client))

    assert response.status_code == 200
    assert response.json()["id"] == str(salons.with_master_by_client.id)


async def test_the_master_reads_a_booking_with_them(
    app: FastAPI, authorize: AuthorizationFactory, salons: Salons
) -> None:
    response = await read(app, salons.with_master.id, as_master(authorize, salons))

    assert response.status_code == 200


async def test_a_booking_that_is_not_the_callers_is_not_found(
    app: FastAPI, authorize: AuthorizationFactory, salons: Salons
) -> None:
    response = await read(app, salons.with_master.id, authorize(user_id=salons.client))

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


async def test_an_admin_of_another_salon_does_not_find_the_booking(
    app: FastAPI, authorize: AuthorizationFactory, salons: Salons
) -> None:
    response = await read(
        app, salons.in_other_salon.id, authorize(roles=(("salon_admin", salons.salon),))
    )

    assert response.status_code == 404


async def test_reading_without_a_token_is_refused(app: FastAPI, salons: Salons) -> None:
    response = await read(app, salons.with_master.id, {})

    assert response.status_code == 401
