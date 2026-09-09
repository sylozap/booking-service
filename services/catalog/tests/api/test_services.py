"""/api/v1/services and /api/v1/salons/{id}/services through the real ASGI stack."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from barber_catalog.models.master import Master
from barber_catalog.models.master_service import MasterService
from barber_catalog.models.salon import Salon
from barber_catalog.models.service import Service
from barber_common.testing.fixtures import app_client

pytestmark = pytest.mark.integration

SERVICES = "/api/v1/services"

ANOTHER_SALON = uuid4()

SalonFactory = Callable[..., Awaitable[Salon]]
MasterFactory = Callable[..., Awaitable[Master]]
ServiceFactory = Callable[..., Awaitable[Service]]
OfferingFactory = Callable[..., Awaitable[MasterService]]
AuthorizationFactory = Callable[..., dict[str, str]]


def a_service(**overrides: object) -> dict[str, object]:
    """A valid creation body a test can bend one field of."""
    body: dict[str, object] = {
        "name": "Haircut",
        "base_duration_min": 45,
        "base_price": "3500.00",
        "currency": "RUB",
    }
    body.update(overrides)
    return body


def price_list(salon: Salon) -> str:
    return f"/api/v1/salons/{salon.id}/services"


# --- creating ---------------------------------------------------------------


async def test_a_salon_admin_adds_a_service_to_their_salon(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
) -> None:
    salon = await make_salon()
    headers = authorize(roles=(("salon_admin", salon.id),))

    async with app_client(app) as client:
        response = await client.post(price_list(salon), json=a_service(), headers=headers)

    assert response.status_code == 201
    assert response.json()["base_price"] == "3500.00"
    assert response.json()["is_archived"] is False


async def test_a_salon_admin_cannot_add_a_service_to_another_salon(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
) -> None:
    salon = await make_salon()
    headers = authorize(roles=(("salon_admin", ANOTHER_SALON),))

    async with app_client(app) as client:
        response = await client.post(price_list(salon), json=a_service(), headers=headers)

    assert response.status_code == 403


async def test_a_client_cannot_add_a_service(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
) -> None:
    salon = await make_salon()
    headers = authorize(roles=(("client", None),))

    async with app_client(app) as client:
        response = await client.post(price_list(salon), json=a_service(), headers=headers)

    assert response.status_code == 403


async def test_adding_a_service_needs_a_token(
    app: FastAPI,
    make_salon: SalonFactory,
) -> None:
    salon = await make_salon()

    async with app_client(app) as client:
        response = await client.post(price_list(salon), json=a_service())

    assert response.status_code == 401


async def test_a_service_of_no_duration_is_refused(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
) -> None:
    salon = await make_salon()
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.post(
            price_list(salon), json=a_service(base_duration_min=0), headers=headers
        )

    assert response.status_code == 422


async def test_a_negative_price_is_refused(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
) -> None:
    salon = await make_salon()
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.post(
            price_list(salon), json=a_service(base_price="-1.00"), headers=headers
        )

    assert response.status_code == 422


async def test_a_currency_that_is_not_iso_4217_is_refused(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
) -> None:
    """One currency written three ways is three currencies to any sum."""
    salon = await make_salon()
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.post(
            price_list(salon), json=a_service(currency="rubles"), headers=headers
        )

    assert response.status_code == 422


# --- deletion is impossible -------------------------------------------------


async def test_a_service_cannot_be_deleted(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_service: ServiceFactory,
) -> None:
    """Bookings refer to this row; deleting it dangles every one of them.

    There is no handler at all, so this is 405 from the router rather than a
    check that could be forgotten.
    """
    salon = await make_salon()
    service = await make_service(salon_id=salon.id)
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.delete(f"{SERVICES}/{service.id}", headers=headers)

    assert response.status_code == 405


# --- archiving --------------------------------------------------------------


async def test_archiving_hides_a_service_from_the_price_list(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_service: ServiceFactory,
) -> None:
    salon = await make_salon()
    service = await make_service(salon_id=salon.id, name="Haircut")
    headers = authorize(roles=(("salon_admin", salon.id),))

    async with app_client(app) as client:
        archived = await client.post(f"{SERVICES}/{service.id}/archive", headers=headers)
        listing = await client.get(price_list(salon))

    assert archived.status_code == 204
    assert listing.json()["items"] == []


async def test_an_archived_service_stays_readable_by_identifier(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_service: ServiceFactory,
) -> None:
    """A booking from last year holds this identifier and needs a name."""
    salon = await make_salon()
    service = await make_service(salon_id=salon.id, name="Haircut")
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        await client.post(f"{SERVICES}/{service.id}/archive", headers=headers)
        response = await client.get(f"{SERVICES}/{service.id}")

    assert response.status_code == 200
    assert response.json()["name"] == "Haircut"
    assert response.json()["is_archived"] is True


async def test_archiving_twice_is_not_a_failure(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_service: ServiceFactory,
) -> None:
    """The requested state is the state that already holds."""
    salon = await make_salon()
    service = await make_service(salon_id=salon.id)
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        first = await client.post(f"{SERVICES}/{service.id}/archive", headers=headers)
        second = await client.post(f"{SERVICES}/{service.id}/archive", headers=headers)

    assert first.status_code == 204
    assert second.status_code == 204


async def test_a_salon_admin_cannot_archive_a_service_of_another_salon(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_service: ServiceFactory,
) -> None:
    salon = await make_salon()
    service = await make_service(salon_id=salon.id)
    headers = authorize(roles=(("salon_admin", ANOTHER_SALON),))

    async with app_client(app) as client:
        response = await client.post(f"{SERVICES}/{service.id}/archive", headers=headers)

    assert response.status_code == 403


async def test_archiving_needs_a_token(
    app: FastAPI,
    make_salon: SalonFactory,
    make_service: ServiceFactory,
) -> None:
    salon = await make_salon()
    service = await make_service(salon_id=salon.id)

    async with app_client(app) as client:
        response = await client.post(f"{SERVICES}/{service.id}/archive")

    assert response.status_code == 401


async def test_archiving_leaves_the_links_of_masters_alone(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
    make_offering: OfferingFactory,
    session: AsyncSession,
) -> None:
    """Otherwise unarchiving means re-entering every master's overrides."""
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(salon_id=salon.id)
    await make_offering(
        master_id=master.id, service_id=service.id, price_override=Decimal("4200.00")
    )
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        await client.post(f"{SERVICES}/{service.id}/archive", headers=headers)

    link = await session.get(MasterService, (master.id, service.id))
    assert link is not None
    assert link.price_override == Decimal("4200.00")


# --- changing ---------------------------------------------------------------


async def test_a_salon_admin_changes_a_service_of_their_salon(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_service: ServiceFactory,
) -> None:
    salon = await make_salon()
    service = await make_service(salon_id=salon.id)
    headers = authorize(roles=(("salon_admin", salon.id),))

    async with app_client(app) as client:
        response = await client.patch(
            f"{SERVICES}/{service.id}", json={"base_price": "4000.00"}, headers=headers
        )

    assert response.status_code == 200
    assert response.json()["base_price"] == "4000.00"


async def test_a_salon_admin_cannot_change_a_service_of_another_salon(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_service: ServiceFactory,
) -> None:
    salon = await make_salon()
    service = await make_service(salon_id=salon.id)
    headers = authorize(roles=(("salon_admin", ANOTHER_SALON),))

    async with app_client(app) as client:
        response = await client.patch(
            f"{SERVICES}/{service.id}", json={"name": "Mine"}, headers=headers
        )

    assert response.status_code == 403


async def test_changing_the_price_leaves_the_override_of_a_master_alone(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
    make_offering: OfferingFactory,
) -> None:
    """The master keeps charging what they set; only the base moved."""
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(salon_id=salon.id, base_price=Decimal("3500.00"))
    await make_offering(
        master_id=master.id, service_id=service.id, price_override=Decimal("4200.00")
    )
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        await client.patch(
            f"{SERVICES}/{service.id}", json={"base_price": "9000.00"}, headers=headers
        )
        card = await client.get(f"/api/v1/masters/{master.id}")

    offered = card.json()["services"][0]
    assert offered["price"] == "4200.00"
    assert offered["base_price"] == "9000.00"


async def test_changing_the_price_moves_a_master_who_set_no_override(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
    make_offering: OfferingFactory,
) -> None:
    """The other half of COALESCE: no override means the salon's figure."""
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(salon_id=salon.id, base_price=Decimal("3500.00"))
    await make_offering(master_id=master.id, service_id=service.id)
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        await client.patch(
            f"{SERVICES}/{service.id}", json={"base_price": "9000.00"}, headers=headers
        )
        card = await client.get(f"/api/v1/masters/{master.id}")

    assert card.json()["services"][0]["price"] == "9000.00"


async def test_archiving_is_not_reachable_through_an_edit(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_service: ServiceFactory,
) -> None:
    """Withdrawing a service is one operation, not a flag on a general edit."""
    salon = await make_salon()
    service = await make_service(salon_id=salon.id)
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.patch(
            f"{SERVICES}/{service.id}", json={"is_archived": True}, headers=headers
        )

    assert response.status_code == 422


async def test_changing_a_service_that_does_not_exist(
    app: FastAPI,
    authorize: AuthorizationFactory,
) -> None:
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.patch(
            f"{SERVICES}/{uuid4()}", json={"name": "Ghost"}, headers=headers
        )

    assert response.status_code == 404


# --- reading ----------------------------------------------------------------


async def test_the_price_list_is_open_to_anonymous_callers(
    app: FastAPI,
    make_salon: SalonFactory,
    make_service: ServiceFactory,
) -> None:
    salon = await make_salon()
    await make_service(salon_id=salon.id, name="Haircut")

    async with app_client(app) as client:
        response = await client.get(price_list(salon))

    assert response.status_code == 200
    assert [item["name"] for item in response.json()["items"]] == ["Haircut"]


async def test_one_service_is_open_to_anonymous_callers(
    app: FastAPI,
    make_salon: SalonFactory,
    make_service: ServiceFactory,
) -> None:
    salon = await make_salon()
    service = await make_service(salon_id=salon.id)

    async with app_client(app) as client:
        response = await client.get(f"{SERVICES}/{service.id}")

    assert response.status_code == 200


async def test_the_price_list_shows_only_this_salon(
    app: FastAPI,
    make_salon: SalonFactory,
    make_service: ServiceFactory,
) -> None:
    mine = await make_salon(name="Mine")
    theirs = await make_salon(name="Theirs")
    await make_service(salon_id=mine.id, name="Ours")
    await make_service(salon_id=theirs.id, name="Not ours")

    async with app_client(app) as client:
        response = await client.get(price_list(mine))

    assert [item["name"] for item in response.json()["items"]] == ["Ours"]


async def test_the_price_list_of_a_salon_that_does_not_exist(app: FastAPI) -> None:
    async with app_client(app) as client:
        response = await client.get(f"/api/v1/salons/{uuid4()}/services")

    assert response.status_code == 404


async def test_reading_a_service_that_does_not_exist(app: FastAPI) -> None:
    async with app_client(app) as client:
        response = await client.get(f"{SERVICES}/{uuid4()}")

    assert response.status_code == 404


async def test_the_price_list_pages_by_cursor(
    app: FastAPI,
    make_salon: SalonFactory,
    make_service: ServiceFactory,
) -> None:
    salon = await make_salon()
    for index in range(3):
        await make_service(salon_id=salon.id, name=f"Service {index}")

    async with app_client(app) as client:
        first = (await client.get(price_list(salon), params={"limit": 2})).json()
        second = (
            await client.get(price_list(salon), params={"limit": 2, "cursor": first["next_cursor"]})
        ).json()

    names = [item["name"] for item in first["items"] + second["items"]]
    assert names == ["Service 0", "Service 1", "Service 2"]
    assert second["next_cursor"] is None
