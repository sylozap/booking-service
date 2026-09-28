"""/api/v1/masters/{id}/schedule and /settings through the real ASGI stack."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from datetime import date, timedelta
from typing import Protocol
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI

from barber_booking.models.master_settings import MasterSettings
from barber_common.cache import Cache
from barber_common.testing.fixtures import app_client

pytestmark = pytest.mark.integration


class CatalogStub(Protocol):
    """The part of the fake catalog these tests bend."""

    profile_calls: int

    def knows_master(
        self,
        master_id: UUID,
        *,
        salon_id: UUID | None = None,
        user_id: UUID | None = None,
        is_active: bool = True,
    ) -> None: ...


MasterSettingsFactory = Callable[..., Awaitable[MasterSettings]]
AuthorizationFactory = Callable[..., dict[str, str]]

# Far enough ahead that "today" never catches up with the suite.
FUTURE = date(2030, 3, 4)
PAST = date(2020, 3, 2)

WEEKDAYS = [
    {"weekday": 0, "start_time": "10:00:00", "end_time": "14:00:00"},
    {"weekday": 0, "start_time": "15:00:00", "end_time": "20:00:00"},
    {"weekday": 2, "start_time": "12:00:00", "end_time": "22:00:00"},
]


def schedule_path(master_id: UUID) -> str:
    return f"/api/v1/masters/{master_id}/schedule"


def exceptions_path(master_id: UUID) -> str:
    return f"{schedule_path(master_id)}/exceptions"


def admin_of(master: MasterSettings) -> tuple[tuple[str, UUID | None], ...]:
    return (("salon_admin", master.salon_id),)


@pytest.fixture
def app_with_cache(app: FastAPI, cache: Cache) -> Iterator[FastAPI]:
    app.state.cache = cache

    yield app

    app.state.cache = None


# --- who may ----------------------------------------------------------------


async def test_an_admin_of_the_salon_sets_the_schedule_and_reads_it_back(
    app: FastAPI, authorize: AuthorizationFactory, make_master_settings: MasterSettingsFactory
) -> None:
    master = await make_master_settings()
    headers = authorize(roles=admin_of(master))

    async with app_client(app) as client:
        put = await client.put(
            schedule_path(master.master_id), json={"intervals": WEEKDAYS}, headers=headers
        )
        get = await client.get(schedule_path(master.master_id), headers=headers)

    assert put.status_code == 200
    assert get.status_code == 200
    body = get.json()
    assert body["timezone"] == "Europe/Moscow"
    [version] = body["versions"]
    assert version["valid_to"] is None
    assert version["intervals"] == WEEKDAYS


async def test_the_master_sets_their_own_schedule(
    app: FastAPI, authorize: AuthorizationFactory, make_master_settings: MasterSettingsFactory
) -> None:
    master = await make_master_settings()
    headers = authorize(roles=(("master", master.salon_id),), user_id=master.user_id)

    async with app_client(app) as client:
        response = await client.put(
            schedule_path(master.master_id), json={"intervals": WEEKDAYS}, headers=headers
        )

    assert response.status_code == 200


async def test_another_master_of_the_same_salon_is_refused(
    app: FastAPI, authorize: AuthorizationFactory, make_master_settings: MasterSettingsFactory
) -> None:
    master = await make_master_settings()
    headers = authorize(roles=(("master", master.salon_id),), user_id=uuid4())

    async with app_client(app) as client:
        response = await client.put(
            schedule_path(master.master_id), json={"intervals": WEEKDAYS}, headers=headers
        )

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden_for_role"


async def test_an_admin_of_another_salon_is_refused(
    app: FastAPI, authorize: AuthorizationFactory, make_master_settings: MasterSettingsFactory
) -> None:
    master = await make_master_settings()
    headers = authorize(roles=(("salon_admin", uuid4()),))

    async with app_client(app) as client:
        response = await client.get(schedule_path(master.master_id), headers=headers)

    assert response.status_code == 403


async def test_a_client_is_refused(
    app: FastAPI, authorize: AuthorizationFactory, make_master_settings: MasterSettingsFactory
) -> None:
    master = await make_master_settings()

    async with app_client(app) as client:
        response = await client.get(schedule_path(master.master_id), headers=authorize())

    assert response.status_code == 403


async def test_an_anonymous_caller_is_refused(
    app: FastAPI, make_master_settings: MasterSettingsFactory
) -> None:
    master = await make_master_settings()

    async with app_client(app) as client:
        response = await client.get(schedule_path(master.master_id))

    assert response.status_code == 401


async def test_a_super_admin_may_set_any_schedule(
    app: FastAPI, authorize: AuthorizationFactory, make_master_settings: MasterSettingsFactory
) -> None:
    master = await make_master_settings()

    async with app_client(app) as client:
        response = await client.put(
            schedule_path(master.master_id),
            json={"intervals": WEEKDAYS},
            headers=authorize(roles=(("super_admin", None),)),
        )

    assert response.status_code == 200


async def test_an_unknown_master_is_404(app: FastAPI, authorize: AuthorizationFactory) -> None:
    async with app_client(app) as client:
        response = await client.get(
            schedule_path(uuid4()), headers=authorize(roles=(("super_admin", None),))
        )

    assert response.status_code == 404


# --- the weekly template ----------------------------------------------------


async def test_overlapping_intervals_of_one_day_are_422(
    app: FastAPI, authorize: AuthorizationFactory, make_master_settings: MasterSettingsFactory
) -> None:
    master = await make_master_settings()
    overlapping = [
        {"weekday": 0, "start_time": "10:00:00", "end_time": "14:00:00"},
        {"weekday": 0, "start_time": "13:00:00", "end_time": "18:00:00"},
    ]

    async with app_client(app) as client:
        response = await client.put(
            schedule_path(master.master_id),
            json={"intervals": overlapping},
            headers=authorize(roles=admin_of(master)),
        )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_an_interval_that_ends_before_it_starts_is_422(
    app: FastAPI, authorize: AuthorizationFactory, make_master_settings: MasterSettingsFactory
) -> None:
    master = await make_master_settings()

    async with app_client(app) as client:
        response = await client.put(
            schedule_path(master.master_id),
            json={"intervals": [{"weekday": 0, "start_time": "20:00", "end_time": "10:00"}]},
            headers=authorize(roles=admin_of(master)),
        )

    assert response.status_code == 422


async def test_a_template_starting_in_the_past_is_422(
    app: FastAPI, authorize: AuthorizationFactory, make_master_settings: MasterSettingsFactory
) -> None:
    master = await make_master_settings()

    async with app_client(app) as client:
        response = await client.put(
            schedule_path(master.master_id),
            json={"valid_from": PAST.isoformat(), "intervals": WEEKDAYS},
            headers=authorize(roles=admin_of(master)),
        )

    assert response.status_code == 422


async def test_a_template_from_a_future_date_closes_the_current_one(
    app: FastAPI, authorize: AuthorizationFactory, make_master_settings: MasterSettingsFactory
) -> None:
    master = await make_master_settings()
    headers = authorize(roles=admin_of(master))
    later = [{"weekday": 4, "start_time": "09:00:00", "end_time": "18:00:00"}]

    async with app_client(app) as client:
        await client.put(
            schedule_path(master.master_id), json={"intervals": WEEKDAYS}, headers=headers
        )
        response = await client.put(
            schedule_path(master.master_id),
            json={"valid_from": FUTURE.isoformat(), "intervals": later},
            headers=headers,
        )

    current, planned = response.json()["versions"]
    assert current["valid_to"] == (FUTURE - timedelta(days=1)).isoformat()
    assert current["intervals"] == WEEKDAYS
    assert planned == {"valid_from": FUTURE.isoformat(), "valid_to": None, "intervals": later}


async def test_replacing_the_template_leaves_the_exceptions_alone(
    app: FastAPI, authorize: AuthorizationFactory, make_master_settings: MasterSettingsFactory
) -> None:
    master = await make_master_settings()
    headers = authorize(roles=admin_of(master))

    async with app_client(app) as client:
        await client.post(
            exceptions_path(master.master_id),
            json={"effective_on": FUTURE.isoformat(), "kind": "day_off"},
            headers=headers,
        )
        await client.put(
            schedule_path(master.master_id), json={"intervals": WEEKDAYS}, headers=headers
        )
        response = await client.get(
            exceptions_path(master.master_id),
            params={"date_from": FUTURE.isoformat(), "date_to": FUTURE.isoformat()},
            headers=headers,
        )

    [kept] = response.json()["items"]
    assert kept["kind"] == "day_off"


# --- exceptions -------------------------------------------------------------


async def test_a_break_is_added_and_listed(
    app: FastAPI, authorize: AuthorizationFactory, make_master_settings: MasterSettingsFactory
) -> None:
    master = await make_master_settings()
    headers = authorize(roles=admin_of(master))
    body = {
        "effective_on": FUTURE.isoformat(),
        "kind": "break",
        "start_time": "13:00:00",
        "end_time": "14:00:00",
        "reason": "Lunch",
    }

    async with app_client(app) as client:
        created = await client.post(exceptions_path(master.master_id), json=body, headers=headers)
        listed = await client.get(
            exceptions_path(master.master_id),
            params={"date_from": FUTURE.isoformat()},
            headers=headers,
        )

    assert created.status_code == 201
    assert listed.json()["items"] == [created.json()]
    assert created.json()["reason"] == "Lunch"


async def test_an_exception_on_a_past_date_is_422(
    app: FastAPI, authorize: AuthorizationFactory, make_master_settings: MasterSettingsFactory
) -> None:
    master = await make_master_settings()

    async with app_client(app) as client:
        response = await client.post(
            exceptions_path(master.master_id),
            json={"effective_on": PAST.isoformat(), "kind": "day_off"},
            headers=authorize(roles=admin_of(master)),
        )

    assert response.status_code == 422


async def test_a_day_off_on_a_date_with_a_break_is_422(
    app: FastAPI, authorize: AuthorizationFactory, make_master_settings: MasterSettingsFactory
) -> None:
    master = await make_master_settings()
    headers = authorize(roles=admin_of(master))

    async with app_client(app) as client:
        await client.post(
            exceptions_path(master.master_id),
            json={
                "effective_on": FUTURE.isoformat(),
                "kind": "break",
                "start_time": "13:00",
                "end_time": "14:00",
            },
            headers=headers,
        )
        response = await client.post(
            exceptions_path(master.master_id),
            json={"effective_on": FUTURE.isoformat(), "kind": "day_off"},
            headers=headers,
        )

    assert response.status_code == 422


async def test_a_day_off_with_hours_is_422(
    app: FastAPI, authorize: AuthorizationFactory, make_master_settings: MasterSettingsFactory
) -> None:
    master = await make_master_settings()

    async with app_client(app) as client:
        response = await client.post(
            exceptions_path(master.master_id),
            json={
                "effective_on": FUTURE.isoformat(),
                "kind": "day_off",
                "start_time": "10:00",
                "end_time": "12:00",
            },
            headers=authorize(roles=admin_of(master)),
        )

    assert response.status_code == 422


async def test_an_exception_is_withdrawn(
    app: FastAPI, authorize: AuthorizationFactory, make_master_settings: MasterSettingsFactory
) -> None:
    master = await make_master_settings()
    headers = authorize(roles=admin_of(master))

    async with app_client(app) as client:
        created = await client.post(
            exceptions_path(master.master_id),
            json={"effective_on": FUTURE.isoformat(), "kind": "day_off"},
            headers=headers,
        )
        deleted = await client.delete(
            f"{exceptions_path(master.master_id)}/{created.json()['id']}", headers=headers
        )
        listed = await client.get(
            exceptions_path(master.master_id),
            params={"date_from": FUTURE.isoformat()},
            headers=headers,
        )

    assert deleted.status_code == 204
    assert listed.json()["items"] == []


async def test_the_exception_of_another_master_is_404(
    app: FastAPI, authorize: AuthorizationFactory, make_master_settings: MasterSettingsFactory
) -> None:
    salon_id = uuid4()
    owner = await make_master_settings(salon_id=salon_id)
    other = await make_master_settings(salon_id=salon_id)
    headers = authorize(roles=(("salon_admin", salon_id),))

    async with app_client(app) as client:
        created = await client.post(
            exceptions_path(owner.master_id),
            json={"effective_on": FUTURE.isoformat(), "kind": "day_off"},
            headers=headers,
        )
        response = await client.delete(
            f"{exceptions_path(other.master_id)}/{created.json()['id']}", headers=headers
        )

    assert response.status_code == 404


async def test_a_window_of_exceptions_longer_than_a_year_is_422(
    app: FastAPI, authorize: AuthorizationFactory, make_master_settings: MasterSettingsFactory
) -> None:
    master = await make_master_settings()

    async with app_client(app) as client:
        response = await client.get(
            exceptions_path(master.master_id),
            params={
                "date_from": FUTURE.isoformat(),
                "date_to": (FUTURE + timedelta(days=400)).isoformat(),
            },
            headers=authorize(roles=admin_of(master)),
        )

    assert response.status_code == 422


# --- settings ---------------------------------------------------------------


async def test_the_master_sets_their_buffer(
    app: FastAPI, authorize: AuthorizationFactory, make_master_settings: MasterSettingsFactory
) -> None:
    master = await make_master_settings()

    async with app_client(app) as client:
        response = await client.patch(
            f"/api/v1/masters/{master.master_id}/settings",
            json={"buffer_after_min": 15},
            headers=authorize(roles=(("master", master.salon_id),), user_id=master.user_id),
        )

    assert response.status_code == 200
    assert response.json()["buffer_after_min"] == 15


async def test_a_negative_buffer_is_422(
    app: FastAPI, authorize: AuthorizationFactory, make_master_settings: MasterSettingsFactory
) -> None:
    master = await make_master_settings()

    async with app_client(app) as client:
        response = await client.patch(
            f"/api/v1/masters/{master.master_id}/settings",
            json={"buffer_after_min": -5},
            headers=authorize(roles=admin_of(master)),
        )

    assert response.status_code == 422


async def test_a_client_cannot_change_the_buffer(
    app: FastAPI, authorize: AuthorizationFactory, make_master_settings: MasterSettingsFactory
) -> None:
    master = await make_master_settings()

    async with app_client(app) as client:
        response = await client.patch(
            f"/api/v1/masters/{master.master_id}/settings",
            json={"buffer_after_min": 15},
            headers=authorize(),
        )

    assert response.status_code == 403


# --- the cache --------------------------------------------------------------


async def test_a_schedule_change_retires_what_was_cached_about_the_master(
    app_with_cache: FastAPI,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    cache: Cache,
) -> None:
    master = await make_master_settings()
    generation_key = f"booking:generation:master:{master.master_id}"

    async with app_client(app_with_cache) as client:
        await client.put(
            schedule_path(master.master_id),
            json={"intervals": WEEKDAYS},
            headers=authorize(roles=admin_of(master)),
        )

    assert await cache.get(generation_key) == b"1"


# --- a master booking never heard of ----------------------------------------


async def test_the_first_touch_of_an_unknown_master_takes_their_profile_from_catalog(
    app: FastAPI, catalog: CatalogStub, authorize: AuthorizationFactory
) -> None:
    master_id, salon_id = uuid4(), uuid4()
    catalog.knows_master(master_id, salon_id=salon_id)
    headers = authorize(roles=(("salon_admin", salon_id),))

    async with app_client(app) as client:
        put = await client.put(
            schedule_path(master_id), json={"intervals": WEEKDAYS}, headers=headers
        )
        get = await client.get(schedule_path(master_id), headers=headers)

    assert (put.status_code, get.status_code) == (200, 200)
    assert get.json()["timezone"] == "Europe/Moscow"
    # Asked once: after the first touch the settings are booking's own.
    assert catalog.profile_calls == 1


async def test_the_master_is_recognised_by_the_account_catalog_names(
    app: FastAPI, catalog: CatalogStub, authorize: AuthorizationFactory
) -> None:
    master_id, salon_id, user_id = uuid4(), uuid4(), uuid4()
    catalog.knows_master(master_id, salon_id=salon_id, user_id=user_id)

    async with app_client(app) as client:
        response = await client.patch(
            f"/api/v1/masters/{master_id}/settings",
            json={"buffer_after_min": 10},
            headers=authorize(roles=(("master", salon_id),), user_id=user_id),
        )

    assert response.status_code == 200
    assert response.json()["buffer_after_min"] == 10


async def test_an_admin_of_another_salon_is_refused_for_a_master_just_learned_of(
    app: FastAPI, catalog: CatalogStub, authorize: AuthorizationFactory
) -> None:
    master_id = uuid4()
    catalog.knows_master(master_id)

    async with app_client(app) as client:
        response = await client.get(
            schedule_path(master_id), headers=authorize(roles=(("salon_admin", uuid4()),))
        )

    assert response.status_code == 403
