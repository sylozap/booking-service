"""PUT and DELETE /api/v1/masters/{id}/services/{sid} through the real stack.

The arithmetic of COALESCE is settled in tests/unit/test_pricing.py, without a
database. What is tested here is the endpoint around it: who may call it, which
combinations it refuses, and what repeating it does.
"""

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

ANOTHER_SALON = uuid4()

SalonFactory = Callable[..., Awaitable[Salon]]
MasterFactory = Callable[..., Awaitable[Master]]
ServiceFactory = Callable[..., Awaitable[Service]]
OfferingFactory = Callable[..., Awaitable[MasterService]]
AuthorizationFactory = Callable[..., dict[str, str]]


def link_url(master: Master, service: Service) -> str:
    return f"/api/v1/masters/{master.id}/services/{service.id}"


# --- the terms --------------------------------------------------------------


async def test_an_override_is_applied(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(
        salon_id=salon.id, base_price=Decimal("3500.00"), base_duration_min=45
    )
    headers = authorize(roles=(("salon_admin", salon.id),))

    async with app_client(app) as client:
        response = await client.put(
            link_url(master, service),
            json={"price_override": "4200.00", "duration_override": 60},
            headers=headers,
        )

    body = response.json()
    assert response.status_code == 200
    assert body["price"] == "4200.00"
    assert body["duration_min"] == 60
    assert body["base_price"] == "3500.00"
    assert body["base_duration_min"] == 45


async def test_an_empty_body_means_the_salon_figures(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(
        salon_id=salon.id, base_price=Decimal("3500.00"), base_duration_min=45
    )
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.put(link_url(master, service), json={}, headers=headers)

    body = response.json()
    assert body["price"] == "3500.00"
    assert body["duration_min"] == 45
    assert body["price_override"] is None


async def test_removing_an_override_returns_the_base_figure(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
) -> None:
    """PUT states the whole link, so an omitted override is a removed one."""
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(salon_id=salon.id, base_price=Decimal("3500.00"))
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        await client.put(
            link_url(master, service), json={"price_override": "4200.00"}, headers=headers
        )
        response = await client.put(link_url(master, service), json={}, headers=headers)

    assert response.json()["price"] == "3500.00"
    assert response.json()["price_override"] is None


async def test_stating_the_same_link_twice_changes_nothing(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
) -> None:
    """An administrator pricing a dozen masters retries."""
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(salon_id=salon.id)
    headers = authorize(roles=(("super_admin", None),))
    body = {"price_override": "4200.00"}

    async with app_client(app) as client:
        first = await client.put(link_url(master, service), json=body, headers=headers)
        second = await client.put(link_url(master, service), json=body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["price"] == second.json()["price"]


async def test_a_negative_price_override_is_refused(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(salon_id=salon.id)
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.put(
            link_url(master, service), json={"price_override": "-1.00"}, headers=headers
        )

    assert response.status_code == 422


async def test_a_duration_override_of_zero_is_refused(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(salon_id=salon.id)
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.put(
            link_url(master, service), json={"duration_override": 0}, headers=headers
        )

    assert response.status_code == 422


# --- refused combinations ---------------------------------------------------


async def test_a_service_of_another_salon_is_refused(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
) -> None:
    """422 and not 404: both exist, the combination is what is wrong."""
    mine = await make_salon(name="Mine")
    theirs = await make_salon(name="Theirs")
    master = await make_master(salon_id=mine.id)
    service = await make_service(salon_id=theirs.id)
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.put(link_url(master, service), json={}, headers=headers)

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_an_archived_service_cannot_be_taken_up(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
) -> None:
    """Archiving is a state, not an opinion the master may overrule."""
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(salon_id=salon.id, is_archived=True)
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.put(link_url(master, service), json={}, headers=headers)

    assert response.status_code == 422


async def test_a_service_that_does_not_exist(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.put(
            f"/api/v1/masters/{master.id}/services/{uuid4()}", json={}, headers=headers
        )

    assert response.status_code == 404


async def test_a_master_that_does_not_exist(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_service: ServiceFactory,
) -> None:
    salon = await make_salon()
    service = await make_service(salon_id=salon.id)
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.put(
            f"/api/v1/masters/{uuid4()}/services/{service.id}", json={}, headers=headers
        )

    assert response.status_code == 404


# --- who may call it --------------------------------------------------------


async def test_a_salon_admin_prices_a_master_of_their_salon(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(salon_id=salon.id)
    headers = authorize(roles=(("salon_admin", salon.id),))

    async with app_client(app) as client:
        response = await client.put(link_url(master, service), json={}, headers=headers)

    assert response.status_code == 200


async def test_a_salon_admin_cannot_price_a_master_of_another_salon(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(salon_id=salon.id)
    headers = authorize(roles=(("salon_admin", ANOTHER_SALON),))

    async with app_client(app) as client:
        response = await client.put(link_url(master, service), json={}, headers=headers)

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden_for_role"


async def test_a_caller_with_no_rights_cannot_discover_service_identifiers(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
) -> None:
    """403 comes before the service is looked at, so both answers agree.

    Checking the service first would let a caller of another salon tell a real
    service identifier from an invented one by the status code.
    """
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    headers = authorize(roles=(("salon_admin", ANOTHER_SALON),))

    async with app_client(app) as client:
        invented = await client.put(
            f"/api/v1/masters/{master.id}/services/{uuid4()}", json={}, headers=headers
        )

    assert invented.status_code == 403


async def test_a_client_cannot_price_a_master(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(salon_id=salon.id)
    headers = authorize(roles=(("client", None),))

    async with app_client(app) as client:
        response = await client.put(link_url(master, service), json={}, headers=headers)

    assert response.status_code == 403


async def test_pricing_needs_a_token(
    app: FastAPI,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(salon_id=salon.id)

    async with app_client(app) as client:
        response = await client.put(link_url(master, service), json={})

    assert response.status_code == 401


# --- withdrawing ------------------------------------------------------------


async def test_withdrawing_takes_the_service_off_the_card(
    app: FastAPI,
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
    headers = authorize(roles=(("salon_admin", salon.id),))

    async with app_client(app) as client:
        removed = await client.delete(link_url(master, service), headers=headers)
        card = await client.get(f"/api/v1/masters/{master.id}")

    assert removed.status_code == 204
    assert card.json()["services"] == []


async def test_withdrawing_keeps_the_terms_for_later(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
    make_offering: OfferingFactory,
    session: AsyncSession,
) -> None:
    """A master who stops for a month should not be re-priced by hand."""
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(salon_id=salon.id)
    await make_offering(
        master_id=master.id, service_id=service.id, price_override=Decimal("4200.00")
    )
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        await client.delete(link_url(master, service), headers=headers)

    link = await session.get(MasterService, (master.id, service.id))
    assert link is not None
    assert link.is_active is False
    assert link.price_override == Decimal("4200.00")


async def test_stating_a_withdrawn_link_again_brings_it_back(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
    make_offering: OfferingFactory,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(salon_id=salon.id, name="Haircut")
    await make_offering(master_id=master.id, service_id=service.id, is_active=False)
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        await client.put(link_url(master, service), json={}, headers=headers)
        card = await client.get(f"/api/v1/masters/{master.id}")

    assert [item["name"] for item in card.json()["services"]] == ["Haircut"]


async def test_withdrawing_twice_is_not_a_failure(
    app: FastAPI,
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

    async with app_client(app) as client:
        first = await client.delete(link_url(master, service), headers=headers)
        second = await client.delete(link_url(master, service), headers=headers)

    assert first.status_code == 204
    assert second.status_code == 204


async def test_withdrawing_a_link_that_never_existed_is_not_a_failure(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(salon_id=salon.id)
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.delete(link_url(master, service), headers=headers)

    assert response.status_code == 204


async def test_withdrawing_does_not_touch_the_service(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
    make_offering: OfferingFactory,
) -> None:
    """Bookings hold their own snapshot; the price list is unaffected."""
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(salon_id=salon.id, name="Haircut")
    await make_offering(master_id=master.id, service_id=service.id)
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        await client.delete(link_url(master, service), headers=headers)
        listing = await client.get(f"/api/v1/salons/{salon.id}/services")

    assert [item["name"] for item in listing.json()["items"]] == ["Haircut"]


async def test_a_salon_admin_cannot_withdraw_in_another_salon(
    app: FastAPI,
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
    headers = authorize(roles=(("salon_admin", ANOTHER_SALON),))

    async with app_client(app) as client:
        response = await client.delete(link_url(master, service), headers=headers)

    assert response.status_code == 403


async def test_withdrawing_needs_a_token(
    app: FastAPI,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(salon_id=salon.id)

    async with app_client(app) as client:
        response = await client.delete(link_url(master, service))

    assert response.status_code == 401
