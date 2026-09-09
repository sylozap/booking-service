"""GET /internal/v1/masters/{id}/services/{sid} through the real ASGI stack.

This is the call the whole of `booking` rests on, so the test that matters most
here is the one asserting the answer is complete: every field of the shared
contract is present and carries what the database says. It is checked against
the contract model itself rather than a hand-written list, so a field added to
:class:`MasterServiceDetails` fails this test until the endpoint fills it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import FastAPI

from barber_catalog.models.master import Master
from barber_catalog.models.master_service import MasterService
from barber_catalog.models.salon import Salon
from barber_catalog.models.service import Service
from barber_common.contracts.catalog import MasterServiceDetails
from barber_common.testing.fixtures import app_client

pytestmark = pytest.mark.integration

ANOTHER_SALON = uuid4()

SalonFactory = Callable[..., Awaitable[Salon]]
MasterFactory = Callable[..., Awaitable[Master]]
ServiceFactory = Callable[..., Awaitable[Service]]
OfferingFactory = Callable[..., Awaitable[MasterService]]
AuthorizationFactory = Callable[..., dict[str, str]]


def details_url(master: Master, service: Service) -> str:
    return f"/internal/v1/masters/{master.id}/services/{service.id}"


# --- the contract -----------------------------------------------------------


async def test_the_answer_satisfies_the_shared_contract(
    app: FastAPI,
    authorize_service: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
    make_offering: OfferingFactory,
) -> None:
    """Parsed by the same model booking parses it with.

    A field added to the contract and not filled here fails this test, which is
    the point of keeping the schema in the chassis rather than as a dictionary
    at each end.
    """
    salon = await make_salon(
        timezone="Europe/Moscow",
        slot_step_min=15,
        booking_min_lead_min=120,
        booking_horizon_days=60,
        cancel_deadline_min=240,
    )
    master = await make_master(salon_id=salon.id)
    service = await make_service(
        salon_id=salon.id,
        name="Haircut",
        base_price=Decimal("3500.00"),
        base_duration_min=45,
        currency="RUB",
    )
    await make_offering(master_id=master.id, service_id=service.id)

    async with app_client(app) as client:
        response = await client.get(details_url(master, service), headers=authorize_service())

    assert response.status_code == 200
    details = MasterServiceDetails.model_validate(response.json())
    assert details.master_id == master.id
    assert details.salon_id == salon.id
    assert details.master_active is True
    assert details.service_id == service.id
    assert details.service_name == "Haircut"
    assert details.duration_min == 45
    assert details.price == Decimal("3500.00")
    assert details.currency == "RUB"
    assert details.salon.timezone == "Europe/Moscow"
    assert details.salon.slot_step_min == 15
    assert details.salon.booking_min_lead_min == 120
    assert details.salon.booking_horizon_days == 60
    assert details.salon.cancel_deadline_min == 240


async def test_the_price_travels_as_a_string(
    app: FastAPI,
    authorize_service: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
    make_offering: OfferingFactory,
) -> None:
    """Money as a JSON number goes through a float and comes back short."""
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(salon_id=salon.id, base_price=Decimal("3500.10"))
    await make_offering(master_id=master.id, service_id=service.id)

    async with app_client(app) as client:
        response = await client.get(details_url(master, service), headers=authorize_service())

    assert response.json()["price"] == "3500.10"


async def test_the_answer_carries_what_this_master_charges(
    app: FastAPI,
    authorize_service: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
    make_offering: OfferingFactory,
) -> None:
    """Already resolved: booking never sees the two halves of COALESCE."""
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(
        salon_id=salon.id, base_price=Decimal("3500.00"), base_duration_min=45
    )
    await make_offering(
        master_id=master.id,
        service_id=service.id,
        price_override=Decimal("4200.00"),
        duration_override=60,
    )

    async with app_client(app) as client:
        response = await client.get(details_url(master, service), headers=authorize_service())

    body = response.json()
    assert body["price"] == "4200.00"
    assert body["duration_min"] == 60
    assert "base_price" not in body
    assert "price_override" not in body


async def test_the_time_zone_is_an_iana_identifier(
    app: FastAPI,
    authorize_service: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
    make_offering: OfferingFactory,
) -> None:
    """booking unfolds a weekly schedule against this, so it cannot be an offset."""
    salon = await make_salon(timezone="Asia/Yekaterinburg")
    master = await make_master(salon_id=salon.id)
    service = await make_service(salon_id=salon.id)
    await make_offering(master_id=master.id, service_id=service.id)

    async with app_client(app) as client:
        response = await client.get(details_url(master, service), headers=authorize_service())

    assert response.json()["salon"]["timezone"] == "Asia/Yekaterinburg"


# --- states, not errors -----------------------------------------------------


async def test_a_deactivated_master_is_a_flag_and_not_an_error(
    app: FastAPI,
    authorize_service: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
    make_offering: OfferingFactory,
) -> None:
    """It is what lets booking answer master_inactive rather than 502."""
    salon = await make_salon()
    master = await make_master(salon_id=salon.id, is_active=False)
    service = await make_service(salon_id=salon.id)
    await make_offering(master_id=master.id, service_id=service.id)

    async with app_client(app) as client:
        response = await client.get(details_url(master, service), headers=authorize_service())

    assert response.status_code == 200
    assert response.json()["master_active"] is False


async def test_a_master_who_does_not_offer_the_service_is_a_404(
    app: FastAPI,
    authorize_service: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
) -> None:
    """booking turns this into service_not_offered."""
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(salon_id=salon.id)

    async with app_client(app) as client:
        response = await client.get(details_url(master, service), headers=authorize_service())

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


async def test_a_withdrawn_link_is_a_404(
    app: FastAPI,
    authorize_service: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
    make_offering: OfferingFactory,
) -> None:
    """An inactive link is indistinguishable from an absent one here."""
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(salon_id=salon.id)
    await make_offering(master_id=master.id, service_id=service.id, is_active=False)

    async with app_client(app) as client:
        response = await client.get(details_url(master, service), headers=authorize_service())

    assert response.status_code == 404


async def test_a_master_that_does_not_exist_is_a_404(
    app: FastAPI,
    authorize_service: AuthorizationFactory,
    make_salon: SalonFactory,
    make_service: ServiceFactory,
) -> None:
    salon = await make_salon()
    service = await make_service(salon_id=salon.id)

    async with app_client(app) as client:
        response = await client.get(
            f"/internal/v1/masters/{uuid4()}/services/{service.id}",
            headers=authorize_service(),
        )

    assert response.status_code == 404


# --- who may call it --------------------------------------------------------


async def test_a_user_token_is_refused(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
    make_offering: OfferingFactory,
) -> None:
    """Even a super_admin's. /internal is closed to people, not to roles."""
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(salon_id=salon.id)
    await make_offering(master_id=master.id, service_id=service.id)

    async with app_client(app) as client:
        response = await client.get(
            details_url(master, service),
            headers=authorize(roles=(("super_admin", None),)),
        )

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


async def test_a_service_token_without_the_scope_is_refused(
    app: FastAPI,
    authorize_service: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
    make_offering: OfferingFactory,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(salon_id=salon.id)
    await make_offering(master_id=master.id, service_id=service.id)

    async with app_client(app) as client:
        response = await client.get(
            details_url(master, service),
            headers=authorize_service(scopes=("notification:read",)),
        )

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden_for_role"


async def test_no_token_at_all_is_refused(
    app: FastAPI,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
    make_offering: OfferingFactory,
) -> None:
    """Unlike the catalog itself, this is not open to anonymous callers."""
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(salon_id=salon.id)
    await make_offering(master_id=master.id, service_id=service.id)

    async with app_client(app) as client:
        response = await client.get(details_url(master, service))

    assert response.status_code == 401


async def test_the_internal_endpoint_is_not_in_the_public_openapi(app: FastAPI) -> None:
    """It is not part of the published contract and not proxied by the gateway."""
    async with app_client(app) as client:
        document = (await client.get("/openapi.json")).json()

    assert not any(path.startswith("/internal") for path in document["paths"])
