"""POST /api/v1/masters/{id}/deactivate and .../activate.

Deactivation is the one write in the catalog whose consequences leave the
service: ``booking`` consumes ``master.deactivated`` and cancels every future
booking of that master. So the assertions here are mostly about the event --
that there is exactly one, that it carries the right partitioning key, and that
a repeated call does not produce a second.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import uuid4

import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from barber_catalog.models.master import Master
from barber_catalog.models.salon import Salon
from barber_common.events.catalog import CATALOG_MASTERS_TOPIC, MasterEventType, MasterUpdated
from barber_common.outbox.models import OutboxMessage
from barber_common.testing.fixtures import app_client

pytestmark = pytest.mark.integration

ANOTHER_SALON = uuid4()

SalonFactory = Callable[..., Awaitable[Salon]]
MasterFactory = Callable[..., Awaitable[Master]]
AuthorizationFactory = Callable[..., dict[str, str]]


def deactivate_url(master: Master) -> str:
    return f"/api/v1/masters/{master.id}/deactivate"


def activate_url(master: Master) -> str:
    return f"/api/v1/masters/{master.id}/activate"


async def queued_events(session: AsyncSession) -> list[OutboxMessage]:
    """Everything waiting in the outbox of this transaction."""
    statement = select(OutboxMessage).order_by(OutboxMessage.created_at)
    return list((await session.execute(statement)).scalars().all())


# --- deactivating -----------------------------------------------------------


async def test_deactivating_answers_202_because_the_cascade_has_not_happened(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
) -> None:
    """The row is written; the cancellations in booking are not, yet."""
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    headers = authorize(roles=(("salon_admin", salon.id),))

    async with app_client(app) as client:
        response = await client.post(deactivate_url(master), headers=headers)

    assert response.status_code == 202
    assert response.json()["is_active"] is False


async def test_deactivating_queues_master_deactivated(
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
        await client.post(deactivate_url(master), headers=headers)

    events = await queued_events(session)
    assert len(events) == 1
    assert events[0].event_type == MasterEventType.DEACTIVATED.value
    assert events[0].topic == CATALOG_MASTERS_TOPIC
    assert events[0].payload["master_id"] == str(master.id)
    assert events[0].payload["salon_id"] == str(salon.id)


async def test_the_partitioning_key_is_the_master(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    session: AsyncSession,
) -> None:
    """A deactivation overtaking the creation would cascade over nothing."""
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        await client.post(deactivate_url(master), headers=headers)

    events = await queued_events(session)
    assert events[0].aggregate_id == master.id


async def test_deactivating_twice_produces_one_event(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    session: AsyncSession,
) -> None:
    """A second event would make booking cancel a second time.

    That is not a wasted message: a booking reinstated between the two calls
    would be cancelled again without anyone having asked for it.
    """
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        first = await client.post(deactivate_url(master), headers=headers)
        second = await client.post(deactivate_url(master), headers=headers)

    assert first.status_code == 202
    assert second.status_code == 202
    assert len(await queued_events(session)) == 1


async def test_a_deactivated_master_leaves_the_active_listing(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id, display_name="Ivan")
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        await client.post(deactivate_url(master), headers=headers)
        listing = await client.get(
            f"/api/v1/salons/{salon.id}/masters", params={"is_active": "true"}
        )

    assert listing.json()["items"] == []


async def test_a_deactivated_master_stays_readable(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
) -> None:
    """Their past bookings still name them."""
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        await client.post(deactivate_url(master), headers=headers)
        card = await client.get(f"/api/v1/masters/{master.id}")

    assert card.status_code == 200
    assert card.json()["is_active"] is False


# --- reactivating -----------------------------------------------------------


async def test_activating_makes_a_master_bookable_again(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id, is_active=False)
    headers = authorize(roles=(("salon_admin", salon.id),))

    async with app_client(app) as client:
        response = await client.post(activate_url(master), headers=headers)

    assert response.status_code == 200
    assert response.json()["is_active"] is True


async def test_activating_does_not_undo_the_cascade(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    session: AsyncSession,
) -> None:
    """Reactivation is announced, but nothing announces the bookings coming back.

    Reinstating them would double-book whichever slots were taken in the
    meantime -- the invariant the whole platform is built around -- and would
    give clients an appointment they were told they no longer had. This test
    is what stops someone adding such an event later without noticing.

    The ``master.updated`` that follows is a snapshot of the profile, not a
    restoration: ``booking`` cancels on ``master.deactivated`` alone, and a
    snapshot saying the master works again says nothing about what was
    cancelled while they did not.
    """
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        await client.post(deactivate_url(master), headers=headers)
        await client.post(activate_url(master), headers=headers)

    events = await queued_events(session)
    assert [event.event_type for event in events] == [
        MasterEventType.DEACTIVATED.value,
        MasterEventType.UPDATED.value,
    ]
    assert MasterUpdated.model_validate(events[1].payload).is_active is True


async def test_activating_twice_is_not_a_failure(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id, is_active=False)
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        first = await client.post(activate_url(master), headers=headers)
        second = await client.post(activate_url(master), headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200


async def test_activity_cannot_be_changed_through_a_general_edit(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
) -> None:
    """A cascade must not hang off a field of a profile edit.

    A client sending the whole profile back would otherwise cancel every
    booking the master has, by accident.
    """
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.patch(
            f"/api/v1/masters/{master.id}", json={"is_active": False}, headers=headers
        )

    assert response.status_code == 422


# --- who may call it --------------------------------------------------------


async def test_a_salon_admin_cannot_deactivate_in_another_salon(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    session: AsyncSession,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    headers = authorize(roles=(("salon_admin", ANOTHER_SALON),))

    async with app_client(app) as client:
        response = await client.post(deactivate_url(master), headers=headers)

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden_for_role"
    assert await queued_events(session) == []


async def test_a_client_cannot_deactivate_a_master(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    headers = authorize(roles=(("client", None),))

    async with app_client(app) as client:
        response = await client.post(deactivate_url(master), headers=headers)

    assert response.status_code == 403


async def test_deactivating_needs_a_token(
    app: FastAPI,
    make_salon: SalonFactory,
    make_master: MasterFactory,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)

    async with app_client(app) as client:
        response = await client.post(deactivate_url(master))

    assert response.status_code == 401


async def test_a_salon_admin_cannot_activate_in_another_salon(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id, is_active=False)
    headers = authorize(roles=(("salon_admin", ANOTHER_SALON),))

    async with app_client(app) as client:
        response = await client.post(activate_url(master), headers=headers)

    assert response.status_code == 403


async def test_deactivating_a_master_that_does_not_exist(
    app: FastAPI,
    authorize: AuthorizationFactory,
) -> None:
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.post(f"/api/v1/masters/{uuid4()}/deactivate", headers=headers)

    assert response.status_code == 404


async def test_activating_a_master_that_does_not_exist(
    app: FastAPI,
    authorize: AuthorizationFactory,
) -> None:
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.post(f"/api/v1/masters/{uuid4()}/activate", headers=headers)

    assert response.status_code == 404


# --- the cache --------------------------------------------------------------


async def test_deactivating_invalidates_the_cached_card(
    app_with_cache: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app_with_cache) as client:
        await client.get(f"/api/v1/masters/{master.id}")
        await client.post(deactivate_url(master), headers=headers)
        card = await client.get(f"/api/v1/masters/{master.id}")

    assert card.json()["is_active"] is False
