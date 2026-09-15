"""Events written to the outbox by every change to the catalog.

For each change: exactly one event, keyed by its aggregate, and none when the
change is refused or rolled back. Publishing to Kafka is covered by the chassis
tests.
"""

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
from barber_common.events.catalog import (
    CATALOG_MASTERS_TOPIC,
    CATALOG_SERVICES_TOPIC,
    MasterCreated,
    MasterDeactivated,
    MasterEventType,
    MasterUpdated,
    ServiceArchived,
    ServiceCreated,
    ServiceEventType,
    ServiceUpdated,
)
from barber_common.outbox.models import OutboxMessage
from barber_common.testing.fixtures import app_client

pytestmark = pytest.mark.integration

ANOTHER_SALON = uuid4()

SalonFactory = Callable[..., Awaitable[Salon]]
MasterFactory = Callable[..., Awaitable[Master]]
ServiceFactory = Callable[..., Awaitable[Service]]
OfferingFactory = Callable[..., Awaitable[MasterService]]
AuthorizationFactory = Callable[..., dict[str, str]]


async def queued_events(session: AsyncSession) -> list[OutboxMessage]:
    """Everything waiting in the outbox of this transaction."""
    statement = select(OutboxMessage).order_by(OutboxMessage.created_at)
    return list((await session.execute(statement)).scalars().all())


async def one_event(session: AsyncSession) -> OutboxMessage:
    """The single event a change is supposed to have produced."""
    events = await queued_events(session)
    assert len(events) == 1, [event.event_type for event in events]
    return events[0]


def a_service_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "name": "Haircut",
        "base_duration_min": 45,
        "base_price": "3500.00",
        "currency": "RUB",
    }
    body.update(overrides)
    return body


# --- masters ----------------------------------------------------------------


async def test_creating_a_master_publishes_master_created(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    session: AsyncSession,
) -> None:
    salon = await make_salon(timezone="Asia/Yekaterinburg")
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.post(
            "/api/v1/masters",
            json={
                "salon_id": str(salon.id),
                "user_id": str(uuid4()),
                "display_name": "Ivan",
            },
            headers=headers,
        )

    event = await one_event(session)
    payload = MasterCreated.model_validate(event.payload)
    assert event.event_type == MasterEventType.CREATED.value
    assert event.topic == CATALOG_MASTERS_TOPIC
    assert event.aggregate_id == payload.master_id
    assert str(payload.master_id) == response.json()["id"]
    assert payload.timezone == "Asia/Yekaterinburg"


async def test_editing_a_master_publishes_master_updated(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    session: AsyncSession,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id, display_name="Ivan")
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        await client.patch(
            f"/api/v1/masters/{master.id}", json={"display_name": "Ivan P."}, headers=headers
        )

    event = await one_event(session)
    payload = MasterUpdated.model_validate(event.payload)
    assert event.event_type == MasterEventType.UPDATED.value
    assert event.topic == CATALOG_MASTERS_TOPIC
    assert event.aggregate_id == master.id
    assert payload.display_name == "Ivan P."


async def test_the_update_payload_is_a_whole_snapshot(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    session: AsyncSession,
) -> None:
    """A consumer applying it is idempotent, and can join the topic late."""
    salon = await make_salon()
    master = await make_master(salon_id=salon.id, specialization="Barber")
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        await client.patch(
            f"/api/v1/masters/{master.id}", json={"bio": "Ten years"}, headers=headers
        )

    payload = MasterUpdated.model_validate((await one_event(session)).payload)
    assert payload.salon_id == salon.id
    assert payload.user_id == master.user_id
    assert payload.specialization == "Barber"
    assert payload.is_active is True


async def test_an_edit_that_changes_nothing_publishes_nothing(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    session: AsyncSession,
) -> None:
    """A form saved without an edit sends the whole body back."""
    salon = await make_salon()
    master = await make_master(salon_id=salon.id, display_name="Ivan")
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.patch(
            f"/api/v1/masters/{master.id}", json={"display_name": "Ivan"}, headers=headers
        )

    assert response.status_code == 200
    assert await queued_events(session) == []


async def test_an_empty_edit_publishes_nothing(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    session: AsyncSession,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        await client.patch(f"/api/v1/masters/{master.id}", json={}, headers=headers)

    assert await queued_events(session) == []


async def test_deactivating_publishes_master_deactivated(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    session: AsyncSession,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        await client.post(f"/api/v1/masters/{master.id}/deactivate", headers=headers)

    event = await one_event(session)
    payload = MasterDeactivated.model_validate(event.payload)
    assert event.event_type == MasterEventType.DEACTIVATED.value
    assert event.aggregate_id == master.id
    assert payload.salon_id == salon.id


async def test_reactivating_publishes_master_updated(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    session: AsyncSession,
) -> None:
    """An update and not an activation of its own: the topic has three types."""
    salon = await make_salon()
    master = await make_master(salon_id=salon.id, is_active=False)
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        await client.post(f"/api/v1/masters/{master.id}/activate", headers=headers)

    event = await one_event(session)
    assert event.event_type == MasterEventType.UPDATED.value
    assert MasterUpdated.model_validate(event.payload).is_active is True


async def test_reactivating_an_active_master_publishes_nothing(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    session: AsyncSession,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        await client.post(f"/api/v1/masters/{master.id}/activate", headers=headers)

    assert await queued_events(session) == []


# --- services ---------------------------------------------------------------


async def test_creating_a_service_publishes_service_created(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    session: AsyncSession,
) -> None:
    salon = await make_salon()
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.post(
            f"/api/v1/salons/{salon.id}/services", json=a_service_body(), headers=headers
        )

    event = await one_event(session)
    payload = ServiceCreated.model_validate(event.payload)
    assert event.event_type == ServiceEventType.CREATED.value
    assert event.topic == CATALOG_SERVICES_TOPIC
    assert event.aggregate_id == payload.service_id
    assert str(payload.service_id) == response.json()["id"]
    assert payload.base_price == Decimal("3500.00")


async def test_the_partitioning_key_of_a_service_event_is_the_service(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_service: ServiceFactory,
    session: AsyncSession,
) -> None:
    """Not the salon: two edits to one service have to stay in order."""
    salon = await make_salon()
    service = await make_service(salon_id=salon.id)
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        await client.patch(
            f"/api/v1/services/{service.id}", json={"name": "Renamed"}, headers=headers
        )

    assert (await one_event(session)).aggregate_id == service.id


async def test_editing_a_service_publishes_service_updated(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_service: ServiceFactory,
    session: AsyncSession,
) -> None:
    salon = await make_salon()
    service = await make_service(salon_id=salon.id, base_price=Decimal("3500.00"))
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        await client.patch(
            f"/api/v1/services/{service.id}", json={"base_price": "9000.00"}, headers=headers
        )

    event = await one_event(session)
    payload = ServiceUpdated.model_validate(event.payload)
    assert event.event_type == ServiceEventType.UPDATED.value
    assert payload.base_price == Decimal("9000.00")
    assert payload.name == service.name


async def test_a_service_edit_that_changes_nothing_publishes_nothing(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_service: ServiceFactory,
    session: AsyncSession,
) -> None:
    salon = await make_salon()
    service = await make_service(salon_id=salon.id, name="Haircut")
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        await client.patch(
            f"/api/v1/services/{service.id}", json={"name": "Haircut"}, headers=headers
        )

    assert await queued_events(session) == []


async def test_archiving_a_service_publishes_service_archived(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_service: ServiceFactory,
    session: AsyncSession,
) -> None:
    salon = await make_salon()
    service = await make_service(salon_id=salon.id)
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        await client.post(f"/api/v1/services/{service.id}/archive", headers=headers)

    event = await one_event(session)
    payload = ServiceArchived.model_validate(event.payload)
    assert event.event_type == ServiceEventType.ARCHIVED.value
    assert event.aggregate_id == service.id
    assert payload.salon_id == salon.id


async def test_archiving_twice_publishes_one_event(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_service: ServiceFactory,
    session: AsyncSession,
) -> None:
    salon = await make_salon()
    service = await make_service(salon_id=salon.id)
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        await client.post(f"/api/v1/services/{service.id}/archive", headers=headers)
        await client.post(f"/api/v1/services/{service.id}/archive", headers=headers)

    await one_event(session)


# --- offerings --------------------------------------------------------------


async def test_offering_a_service_publishes_master_updated(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
    session: AsyncSession,
) -> None:
    """Changing what a master offers publishes ``master.updated``."""
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(salon_id=salon.id)
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        await client.put(
            f"/api/v1/masters/{master.id}/services/{service.id}",
            json={"price_override": "4200.00"},
            headers=headers,
        )

    event = await one_event(session)
    assert event.event_type == MasterEventType.UPDATED.value
    assert event.topic == CATALOG_MASTERS_TOPIC
    assert event.aggregate_id == master.id


async def test_withdrawing_a_service_publishes_master_updated(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
    make_offering: OfferingFactory,
    session: AsyncSession,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(salon_id=salon.id)
    await make_offering(master_id=master.id, service_id=service.id)
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        await client.delete(f"/api/v1/masters/{master.id}/services/{service.id}", headers=headers)

    event = await one_event(session)
    assert event.event_type == MasterEventType.UPDATED.value
    assert event.aggregate_id == master.id


async def test_withdrawing_a_link_that_was_never_there_publishes_nothing(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
    session: AsyncSession,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(salon_id=salon.id)
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        await client.delete(f"/api/v1/masters/{master.id}/services/{service.id}", headers=headers)

    assert await queued_events(session) == []


# --- nothing survives a failed change ---------------------------------------


async def test_a_refused_creation_leaves_no_event(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    session: AsyncSession,
) -> None:
    salon = await make_salon()
    headers = authorize(roles=(("salon_admin", ANOTHER_SALON),))

    async with app_client(app) as client:
        response = await client.post(
            f"/api/v1/salons/{salon.id}/services", json=a_service_body(), headers=headers
        )

    assert response.status_code == 403
    assert await queued_events(session) == []


async def test_a_rolled_back_change_leaves_no_event(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
    session: AsyncSession,
) -> None:
    """A rolled back change leaves no event.

    Linking a service of another salon fails after the link row has been
    staged, so the rollback has to take the event with it.
    """
    mine = await make_salon(name="Mine")
    theirs = await make_salon(name="Theirs")
    master = await make_master(salon_id=mine.id)
    service = await make_service(salon_id=theirs.id)
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.put(
            f"/api/v1/masters/{master.id}/services/{service.id}", json={}, headers=headers
        )

    assert response.status_code == 422
    assert await queued_events(session) == []
