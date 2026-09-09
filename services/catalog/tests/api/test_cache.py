"""Reading the catalog through Redis.

A cache hit is proven from the outside, without instrumenting anything: the row
is changed **behind the scenario's back**, through the session directly, and
the endpoint is asked again. A read that still answers with the old value can
only have come from the cache. A read that answers with the new one went to the
database.

That is also why these tests never use the scenario to make the second change:
going through the endpoint would invalidate, which is the behaviour under test
in the other half of the file.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from barber_catalog.models.master import Master
from barber_catalog.models.master_service import MasterService
from barber_catalog.models.salon import Salon
from barber_catalog.models.service import Service
from barber_catalog.services.cache import GENERATION_KEY
from barber_common.cache import Cache
from barber_common.testing.fixtures import app_client

pytestmark = pytest.mark.integration

SalonFactory = Callable[..., Awaitable[Salon]]
MasterFactory = Callable[..., Awaitable[Master]]
ServiceFactory = Callable[..., Awaitable[Service]]
OfferingFactory = Callable[..., Awaitable[MasterService]]
AuthorizationFactory = Callable[..., dict[str, str]]


async def rename_behind_the_scenario(session: AsyncSession, service: Service, name: str) -> None:
    """Change a row without going through an endpoint.

    The only way to tell a cached answer from a fresh one without reaching
    inside the service: the database now disagrees with the cache, and which
    one the endpoint reports is the answer.
    """
    service.name = name
    await session.flush()


# --- hits and misses --------------------------------------------------------


async def test_the_first_read_of_a_service_reaches_the_database(
    app_with_cache: FastAPI,
    make_salon: SalonFactory,
    make_service: ServiceFactory,
) -> None:
    salon = await make_salon()
    service = await make_service(salon_id=salon.id, name="Haircut")

    async with app_client(app_with_cache) as client:
        response = await client.get(f"/api/v1/services/{service.id}")

    assert response.status_code == 200
    assert response.json()["name"] == "Haircut"


async def test_the_second_read_of_a_service_is_served_from_the_cache(
    app_with_cache: FastAPI,
    make_salon: SalonFactory,
    make_service: ServiceFactory,
    session: AsyncSession,
) -> None:
    salon = await make_salon()
    service = await make_service(salon_id=salon.id, name="Haircut")

    async with app_client(app_with_cache) as client:
        await client.get(f"/api/v1/services/{service.id}")
        await rename_behind_the_scenario(session, service, "Renamed in the database")
        second = await client.get(f"/api/v1/services/{service.id}")

    assert second.json()["name"] == "Haircut"


async def test_the_second_read_of_a_master_card_is_served_from_the_cache(
    app_with_cache: FastAPI,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    session: AsyncSession,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id, display_name="Ivan")

    async with app_client(app_with_cache) as client:
        await client.get(f"/api/v1/masters/{master.id}")
        master.display_name = "Renamed in the database"
        await session.flush()
        second = await client.get(f"/api/v1/masters/{master.id}")

    assert second.json()["display_name"] == "Ivan"


async def test_the_second_read_of_the_internal_answer_is_served_from_the_cache(
    app_with_cache: FastAPI,
    authorize_service: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
    make_offering: OfferingFactory,
    session: AsyncSession,
) -> None:
    """The hot path: booking reads this before every availability calculation."""
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(salon_id=salon.id, base_price=Decimal("3500.00"))
    await make_offering(master_id=master.id, service_id=service.id)
    url = f"/internal/v1/masters/{master.id}/services/{service.id}"

    async with app_client(app_with_cache) as client:
        await client.get(url, headers=authorize_service())
        service.base_price = Decimal("9000.00")
        await session.flush()
        second = await client.get(url, headers=authorize_service())

    assert second.json()["price"] == "3500.00"


async def test_two_services_do_not_share_a_cache_entry(
    app_with_cache: FastAPI,
    make_salon: SalonFactory,
    make_service: ServiceFactory,
) -> None:
    """The key carries the identifier, which this proves rather than assumes."""
    salon = await make_salon()
    first = await make_service(salon_id=salon.id, name="Haircut")
    second = await make_service(salon_id=salon.id, name="Shave")

    async with app_client(app_with_cache) as client:
        await client.get(f"/api/v1/services/{first.id}")
        response = await client.get(f"/api/v1/services/{second.id}")

    assert response.json()["name"] == "Shave"


async def test_a_missing_service_is_not_cached_as_missing(
    app_with_cache: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
) -> None:
    """Otherwise the creation would be invisible for the rest of the window."""
    salon = await make_salon()
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app_with_cache) as client:
        created = await client.post(
            f"/api/v1/salons/{salon.id}/services",
            json={
                "name": "Haircut",
                "base_duration_min": 45,
                "base_price": "3500.00",
                "currency": "RUB",
            },
            headers=headers,
        )
        response = await client.get(f"/api/v1/services/{created.json()['id']}")

    assert response.status_code == 200


# --- invalidation -----------------------------------------------------------


async def test_a_patch_invalidates_the_cached_service(
    app_with_cache: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_service: ServiceFactory,
) -> None:
    salon = await make_salon()
    service = await make_service(salon_id=salon.id, name="Haircut")
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app_with_cache) as client:
        await client.get(f"/api/v1/services/{service.id}")
        await client.patch(
            f"/api/v1/services/{service.id}", json={"name": "Renamed"}, headers=headers
        )
        response = await client.get(f"/api/v1/services/{service.id}")

    assert response.json()["name"] == "Renamed"


async def test_a_patch_to_a_master_invalidates_their_card(
    app_with_cache: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id, display_name="Ivan")
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app_with_cache) as client:
        await client.get(f"/api/v1/masters/{master.id}")
        await client.patch(
            f"/api/v1/masters/{master.id}", json={"display_name": "Ivan P."}, headers=headers
        )
        response = await client.get(f"/api/v1/masters/{master.id}")

    assert response.json()["display_name"] == "Ivan P."


async def test_a_price_change_reaches_the_internal_answer(
    app_with_cache: FastAPI,
    authorize: AuthorizationFactory,
    authorize_service: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
    make_offering: OfferingFactory,
) -> None:
    """The case a per-entity invalidation would get wrong.

    The write is to a service; the stale entry is under a key naming a master
    and a service. A generation counter cannot miss it.
    """
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(salon_id=salon.id, base_price=Decimal("3500.00"))
    await make_offering(master_id=master.id, service_id=service.id)
    url = f"/internal/v1/masters/{master.id}/services/{service.id}"
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app_with_cache) as client:
        await client.get(url, headers=authorize_service())
        await client.patch(
            f"/api/v1/services/{service.id}", json={"base_price": "9000.00"}, headers=headers
        )
        response = await client.get(url, headers=authorize_service())

    assert response.json()["price"] == "9000.00"


async def test_changing_a_salon_policy_reaches_the_internal_answer(
    app_with_cache: FastAPI,
    authorize: AuthorizationFactory,
    authorize_service: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
    make_offering: OfferingFactory,
) -> None:
    """booking lays slots on this grid; a stale step misplaces every one."""
    salon = await make_salon(slot_step_min=15)
    master = await make_master(salon_id=salon.id)
    service = await make_service(salon_id=salon.id)
    await make_offering(master_id=master.id, service_id=service.id)
    url = f"/internal/v1/masters/{master.id}/services/{service.id}"
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app_with_cache) as client:
        await client.get(url, headers=authorize_service())
        await client.patch(
            f"/api/v1/salons/{salon.id}", json={"slot_step_min": 30}, headers=headers
        )
        response = await client.get(url, headers=authorize_service())

    assert response.json()["salon"]["slot_step_min"] == 30


async def test_linking_a_service_invalidates_the_card(
    app_with_cache: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(salon_id=salon.id, name="Haircut")
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app_with_cache) as client:
        await client.get(f"/api/v1/masters/{master.id}")
        await client.put(
            f"/api/v1/masters/{master.id}/services/{service.id}", json={}, headers=headers
        )
        response = await client.get(f"/api/v1/masters/{master.id}")

    assert [item["name"] for item in response.json()["services"]] == ["Haircut"]


async def test_unlinking_a_service_invalidates_the_card(
    app_with_cache: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
    make_offering: OfferingFactory,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(salon_id=salon.id)
    await make_offering(master_id=master.id, service_id=service.id)
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app_with_cache) as client:
        await client.get(f"/api/v1/masters/{master.id}")
        await client.delete(f"/api/v1/masters/{master.id}/services/{service.id}", headers=headers)
        response = await client.get(f"/api/v1/masters/{master.id}")

    assert response.json()["services"] == []


async def test_archiving_a_service_invalidates_it(
    app_with_cache: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_service: ServiceFactory,
) -> None:
    salon = await make_salon()
    service = await make_service(salon_id=salon.id)
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app_with_cache) as client:
        await client.get(f"/api/v1/services/{service.id}")
        await client.post(f"/api/v1/services/{service.id}/archive", headers=headers)
        response = await client.get(f"/api/v1/services/{service.id}")

    assert response.json()["is_archived"] is True


async def test_one_write_bumps_the_generation_once(
    app_with_cache: FastAPI,
    authorize: AuthorizationFactory,
    cache: Cache,
    make_salon: SalonFactory,
    make_service: ServiceFactory,
) -> None:
    """Invalidation is one command per write, whatever changed."""
    salon = await make_salon()
    service = await make_service(salon_id=salon.id)
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app_with_cache) as client:
        await client.get(f"/api/v1/services/{service.id}")
        before = await cache.get(GENERATION_KEY)
        await client.patch(
            f"/api/v1/services/{service.id}", json={"name": "Renamed"}, headers=headers
        )
        after = await cache.get(GENERATION_KEY)

    assert int(after or 0) == int(before or 0) + 1


async def test_a_refused_write_does_not_invalidate(
    app_with_cache: FastAPI,
    authorize: AuthorizationFactory,
    cache: Cache,
    make_salon: SalonFactory,
    make_service: ServiceFactory,
) -> None:
    """Nothing changed, so nothing has to be re-read."""
    salon = await make_salon()
    service = await make_service(salon_id=salon.id)
    headers = authorize(roles=(("client", None),))

    async with app_client(app_with_cache) as client:
        await client.get(f"/api/v1/services/{service.id}")
        before = await cache.get(GENERATION_KEY)
        refused = await client.patch(
            f"/api/v1/services/{service.id}", json={"name": "Mine"}, headers=headers
        )
        after = await cache.get(GENERATION_KEY)

    assert refused.status_code == 403
    assert after == before


# --- living without Redis ---------------------------------------------------


async def test_a_disabled_cache_serves_every_read_from_the_database(
    app: FastAPI,
    make_salon: SalonFactory,
    make_service: ServiceFactory,
    session: AsyncSession,
) -> None:
    """The plain ``app`` fixture has no cache, which is a supported state."""
    salon = await make_salon()
    service = await make_service(salon_id=salon.id, name="Haircut")

    async with app_client(app) as client:
        await client.get(f"/api/v1/services/{service.id}")
        await rename_behind_the_scenario(session, service, "Renamed in the database")
        second = await client.get(f"/api/v1/services/{service.id}")

    assert second.json()["name"] == "Renamed in the database"


async def test_an_unreachable_redis_does_not_break_a_read(
    app: FastAPI,
    make_salon: SalonFactory,
    make_service: ServiceFactory,
    unreachable_cache: Cache,
) -> None:
    """Every request is served, from the database, as though there were none."""
    salon = await make_salon()
    service = await make_service(salon_id=salon.id, name="Haircut")
    app.state.cache = unreachable_cache

    async with app_client(app) as client:
        response = await client.get(f"/api/v1/services/{service.id}")

    assert response.status_code == 200
    assert response.json()["name"] == "Haircut"


async def test_an_unreachable_redis_does_not_break_a_write(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_service: ServiceFactory,
    unreachable_cache: Cache,
) -> None:
    """The invalidation fails silently; the write itself already committed."""
    salon = await make_salon()
    service = await make_service(salon_id=salon.id)
    app.state.cache = unreachable_cache
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.patch(
            f"/api/v1/services/{service.id}", json={"name": "Renamed"}, headers=headers
        )

    assert response.status_code == 200
    assert response.json()["name"] == "Renamed"
