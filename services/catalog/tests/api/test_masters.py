"""/api/v1/masters and /api/v1/salons/{id}/masters through the real ASGI stack."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from barber_catalog.models.master import Master
from barber_catalog.models.master_service import MasterService
from barber_catalog.models.salon import Salon
from barber_catalog.models.service import Service
from barber_common.events.catalog import CATALOG_MASTERS_TOPIC, MasterEventType
from barber_common.outbox.models import OutboxMessage
from barber_common.testing.fixtures import app_client

pytestmark = pytest.mark.integration

MASTERS = "/api/v1/masters"

ANOTHER_SALON = uuid4()

SalonFactory = Callable[..., Awaitable[Salon]]
MasterFactory = Callable[..., Awaitable[Master]]
ServiceFactory = Callable[..., Awaitable[Service]]
OfferingFactory = Callable[..., Awaitable[MasterService]]
AuthorizationFactory = Callable[..., dict[str, str]]


def a_master(salon: Salon, **overrides: object) -> dict[str, object]:
    """A valid creation body a test can bend one field of."""
    body: dict[str, object] = {
        "salon_id": str(salon.id),
        "user_id": str(uuid4()),
        "display_name": "Ivan",
        "specialization": "Barber",
    }
    body.update(overrides)
    return body


async def queued_events(session: AsyncSession) -> list[OutboxMessage]:
    """Everything waiting in the outbox of this transaction."""
    statement = select(OutboxMessage).order_by(OutboxMessage.created_at)
    return list((await session.execute(statement)).scalars().all())


# --- creating ---------------------------------------------------------------


async def test_a_salon_admin_adds_a_master_to_their_salon(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
) -> None:
    salon = await make_salon()
    headers = authorize(roles=(("salon_admin", salon.id),))

    async with app_client(app) as client:
        response = await client.post(MASTERS, json=a_master(salon), headers=headers)

    assert response.status_code == 201
    assert response.json()["display_name"] == "Ivan"


async def test_a_salon_admin_cannot_add_a_master_to_another_salon(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
) -> None:
    salon = await make_salon()
    headers = authorize(roles=(("salon_admin", ANOTHER_SALON),))

    async with app_client(app) as client:
        response = await client.post(MASTERS, json=a_master(salon), headers=headers)

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden_for_role"


async def test_a_client_cannot_add_a_master(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
) -> None:
    salon = await make_salon()
    headers = authorize(roles=(("client", None),))

    async with app_client(app) as client:
        response = await client.post(MASTERS, json=a_master(salon), headers=headers)

    assert response.status_code == 403


async def test_adding_a_master_needs_a_token(
    app: FastAPI,
    make_salon: SalonFactory,
) -> None:
    salon = await make_salon()

    async with app_client(app) as client:
        response = await client.post(MASTERS, json=a_master(salon))

    assert response.status_code == 401


async def test_adding_a_master_to_a_salon_that_does_not_exist(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
) -> None:
    salon = await make_salon()
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.post(
            MASTERS, json=a_master(salon, salon_id=str(uuid4())), headers=headers
        )

    assert response.status_code == 404


async def test_a_second_profile_for_one_account_in_one_salon_is_a_conflict(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
) -> None:
    salon = await make_salon()
    headers = authorize(roles=(("super_admin", None),))
    body = a_master(salon)

    async with app_client(app) as client:
        first = await client.post(MASTERS, json=body, headers=headers)
        second = await client.post(MASTERS, json=body, headers=headers)

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["code"] == "master_profile_exists"


# --- the event --------------------------------------------------------------


async def test_creating_a_master_queues_master_created(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    session: AsyncSession,
) -> None:
    """booking builds master_settings from this event (docs/03-services.md)."""
    salon = await make_salon(timezone="Asia/Yekaterinburg")
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.post(MASTERS, json=a_master(salon), headers=headers)

    events = await queued_events(session)
    assert len(events) == 1
    assert events[0].event_type == MasterEventType.CREATED.value
    assert events[0].topic == CATALOG_MASTERS_TOPIC
    assert events[0].payload["master_id"] == response.json()["id"]
    assert events[0].payload["timezone"] == "Asia/Yekaterinburg"


async def test_the_partitioning_key_is_the_master(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    session: AsyncSession,
) -> None:
    """Everything about one profile has to stay in order on the topic.

    A master.deactivated overtaking the master.created that has not been
    handled yet would cascade over bookings nobody can attach to anything.
    """
    salon = await make_salon()
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.post(MASTERS, json=a_master(salon), headers=headers)

    events = await queued_events(session)
    assert str(events[0].aggregate_id) == response.json()["id"]


async def test_a_refused_creation_leaves_no_event(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    session: AsyncSession,
) -> None:
    """The event and the row are one transaction, so neither survives alone."""
    salon = await make_salon()
    headers = authorize(roles=(("salon_admin", ANOTHER_SALON),))

    async with app_client(app) as client:
        response = await client.post(MASTERS, json=a_master(salon), headers=headers)

    assert response.status_code == 403
    assert await queued_events(session) == []


# --- changing ---------------------------------------------------------------


async def test_a_salon_admin_changes_a_master_of_their_salon(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    headers = authorize(roles=(("salon_admin", salon.id),))

    async with app_client(app) as client:
        response = await client.patch(
            f"{MASTERS}/{master.id}", json={"display_name": "Ivan P."}, headers=headers
        )

    assert response.status_code == 200
    assert response.json()["display_name"] == "Ivan P."


async def test_a_salon_admin_cannot_change_a_master_of_another_salon(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    headers = authorize(roles=(("salon_admin", ANOTHER_SALON),))

    async with app_client(app) as client:
        response = await client.patch(
            f"{MASTERS}/{master.id}", json={"display_name": "Mine"}, headers=headers
        )

    assert response.status_code == 403


async def test_a_master_cannot_be_moved_to_another_salon(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
) -> None:
    """Moving a profile is creating a second one, not editing the first."""
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.patch(
            f"{MASTERS}/{master.id}", json={"salon_id": str(ANOTHER_SALON)}, headers=headers
        )

    assert response.status_code == 422


async def test_changing_a_master_that_does_not_exist(
    app: FastAPI,
    authorize: AuthorizationFactory,
) -> None:
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.patch(
            f"{MASTERS}/{uuid4()}", json={"display_name": "Ghost"}, headers=headers
        )

    assert response.status_code == 404


# --- the card ---------------------------------------------------------------


async def test_the_card_is_open_to_anonymous_callers(
    app: FastAPI,
    make_salon: SalonFactory,
    make_master: MasterFactory,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id, display_name="Ivan")

    async with app_client(app) as client:
        response = await client.get(f"{MASTERS}/{master.id}")

    assert response.status_code == 200
    assert response.json()["display_name"] == "Ivan"
    assert response.json()["services"] == []


async def test_the_card_shows_the_price_this_master_charges(
    app: FastAPI,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
    make_offering: OfferingFactory,
) -> None:
    """The override wins, and the salon's base figures are shown beside it."""
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(
        salon_id=salon.id, name="Haircut", base_price=Decimal("3500.00"), base_duration_min=45
    )
    await make_offering(
        master_id=master.id,
        service_id=service.id,
        price_override=Decimal("4200.00"),
        duration_override=60,
    )

    async with app_client(app) as client:
        response = await client.get(f"{MASTERS}/{master.id}")

    offered = response.json()["services"][0]
    assert offered["price"] == "4200.00"
    assert offered["duration_min"] == 60
    assert offered["base_price"] == "3500.00"
    assert offered["base_duration_min"] == 45


async def test_the_card_falls_back_to_the_salon_figures(
    app: FastAPI,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
    make_offering: OfferingFactory,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(
        salon_id=salon.id, base_price=Decimal("3500.00"), base_duration_min=45
    )
    await make_offering(master_id=master.id, service_id=service.id)

    async with app_client(app) as client:
        response = await client.get(f"{MASTERS}/{master.id}")

    offered = response.json()["services"][0]
    assert offered["price"] == "3500.00"
    assert offered["duration_min"] == 45


async def test_the_card_leaves_out_services_the_master_no_longer_offers(
    app: FastAPI,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
    make_offering: OfferingFactory,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    kept = await make_service(salon_id=salon.id, name="Haircut")
    dropped = await make_service(salon_id=salon.id, name="Shave")
    await make_offering(master_id=master.id, service_id=kept.id)
    await make_offering(master_id=master.id, service_id=dropped.id, is_active=False)

    async with app_client(app) as client:
        response = await client.get(f"{MASTERS}/{master.id}")

    assert [item["name"] for item in response.json()["services"]] == ["Haircut"]


async def test_the_card_orders_services_by_name(
    app: FastAPI,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
    make_offering: OfferingFactory,
) -> None:
    """Unordered, the card reshuffles between requests and reads as noise."""
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    for name in ("Shave", "Haircut", "Beard trim"):
        service = await make_service(salon_id=salon.id, name=name)
        await make_offering(master_id=master.id, service_id=service.id)

    async with app_client(app) as client:
        response = await client.get(f"{MASTERS}/{master.id}")

    assert [item["name"] for item in response.json()["services"]] == [
        "Beard trim",
        "Haircut",
        "Shave",
    ]


async def test_a_deactivated_master_is_still_readable(
    app: FastAPI,
    make_salon: SalonFactory,
    make_master: MasterFactory,
) -> None:
    """Their bookings still name them."""
    salon = await make_salon()
    master = await make_master(salon_id=salon.id, is_active=False)

    async with app_client(app) as client:
        response = await client.get(f"{MASTERS}/{master.id}")

    assert response.status_code == 200
    assert response.json()["is_active"] is False


async def test_reading_a_master_that_does_not_exist(app: FastAPI) -> None:
    async with app_client(app) as client:
        response = await client.get(f"{MASTERS}/{uuid4()}")

    assert response.status_code == 404


# --- the staff listing ------------------------------------------------------


async def test_the_staff_listing_is_open_to_anonymous_callers(
    app: FastAPI,
    make_salon: SalonFactory,
    make_master: MasterFactory,
) -> None:
    salon = await make_salon()
    await make_master(salon_id=salon.id, display_name="Ivan")

    async with app_client(app) as client:
        response = await client.get(f"/api/v1/salons/{salon.id}/masters")

    assert response.status_code == 200
    assert [item["display_name"] for item in response.json()["items"]] == ["Ivan"]


async def test_the_staff_listing_filters_by_activity(
    app: FastAPI,
    make_salon: SalonFactory,
    make_master: MasterFactory,
) -> None:
    salon = await make_salon()
    await make_master(salon_id=salon.id, display_name="Working")
    await make_master(salon_id=salon.id, display_name="Gone", is_active=False)

    async with app_client(app) as client:
        active = await client.get(
            f"/api/v1/salons/{salon.id}/masters", params={"is_active": "true"}
        )
        everyone = await client.get(f"/api/v1/salons/{salon.id}/masters")

    assert [item["display_name"] for item in active.json()["items"]] == ["Working"]
    assert len(everyone.json()["items"]) == 2


async def test_the_staff_listing_shows_only_this_salon(
    app: FastAPI,
    make_salon: SalonFactory,
    make_master: MasterFactory,
) -> None:
    mine = await make_salon(name="Mine")
    theirs = await make_salon(name="Theirs")
    await make_master(salon_id=mine.id, display_name="Ours")
    await make_master(salon_id=theirs.id, display_name="Not ours")

    async with app_client(app) as client:
        response = await client.get(f"/api/v1/salons/{mine.id}/masters")

    assert [item["display_name"] for item in response.json()["items"]] == ["Ours"]


async def test_the_staff_of_a_salon_that_does_not_exist(app: FastAPI) -> None:
    """404 and not an empty page: a typo and an empty salon are different."""
    async with app_client(app) as client:
        response = await client.get(f"/api/v1/salons/{uuid4()}/masters")

    assert response.status_code == 404


async def test_the_staff_listing_pages_by_cursor(
    app: FastAPI,
    make_salon: SalonFactory,
    make_master: MasterFactory,
) -> None:
    salon = await make_salon()
    for index in range(3):
        await make_master(salon_id=salon.id, display_name=f"Master {index}")

    async with app_client(app) as client:
        first = (await client.get(f"/api/v1/salons/{salon.id}/masters", params={"limit": 2})).json()
        second = (
            await client.get(
                f"/api/v1/salons/{salon.id}/masters",
                params={"limit": 2, "cursor": first["next_cursor"]},
            )
        ).json()

    names = [item["display_name"] for item in first["items"] + second["items"]]
    assert names == ["Master 0", "Master 1", "Master 2"]
    assert second["next_cursor"] is None
