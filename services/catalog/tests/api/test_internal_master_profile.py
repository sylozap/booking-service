"""GET /internal/v1/masters/{id} through the real ASGI stack.

The profile booking falls back to for a master whose ``master.created`` it
never received. Parsed with the shared contract, like booking parses it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import uuid4

import pytest
from fastapi import FastAPI

from barber_catalog.models.master import Master
from barber_catalog.models.salon import Salon
from barber_common.contracts.catalog import MasterProfile
from barber_common.testing.fixtures import app_client

pytestmark = pytest.mark.integration

SalonFactory = Callable[..., Awaitable[Salon]]
MasterFactory = Callable[..., Awaitable[Master]]
AuthorizationFactory = Callable[..., dict[str, str]]


def profile_url(master_id: object) -> str:
    return f"/internal/v1/masters/{master_id}"


async def test_the_profile_satisfies_the_shared_contract(
    app: FastAPI,
    authorize_service: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
) -> None:
    salon = await make_salon(timezone="Asia/Yekaterinburg")
    master = await make_master(salon_id=salon.id)

    async with app_client(app) as client:
        response = await client.get(profile_url(master.id), headers=authorize_service())

    assert response.status_code == 200
    assert MasterProfile.model_validate_json(response.content) == MasterProfile(
        master_id=master.id,
        salon_id=salon.id,
        user_id=master.user_id,
        is_active=True,
        timezone="Asia/Yekaterinburg",
    )


async def test_a_deactivated_master_is_described_too(
    app: FastAPI,
    authorize_service: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id, is_active=False)

    async with app_client(app) as client:
        response = await client.get(profile_url(master.id), headers=authorize_service())

    assert response.status_code == 200
    assert response.json()["is_active"] is False


async def test_a_master_that_does_not_exist_is_a_404(
    app: FastAPI, authorize_service: AuthorizationFactory
) -> None:
    async with app_client(app) as client:
        response = await client.get(profile_url(uuid4()), headers=authorize_service())

    assert response.status_code == 404


async def test_a_user_token_is_refused(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
) -> None:
    master = await make_master(salon_id=(await make_salon()).id)

    async with app_client(app) as client:
        response = await client.get(
            profile_url(master.id), headers=authorize(roles=(("super_admin", None),))
        )

    assert response.status_code == 401


async def test_a_service_token_without_the_scope_is_refused(
    app: FastAPI,
    authorize_service: AuthorizationFactory,
    make_salon: SalonFactory,
    make_master: MasterFactory,
) -> None:
    master = await make_master(salon_id=(await make_salon()).id)

    async with app_client(app) as client:
        response = await client.get(
            profile_url(master.id), headers=authorize_service(scopes=("notification:read",))
        )

    assert response.status_code == 403


async def test_no_token_at_all_is_refused(app: FastAPI) -> None:
    async with app_client(app) as client:
        response = await client.get(profile_url(uuid4()))

    assert response.status_code == 401
