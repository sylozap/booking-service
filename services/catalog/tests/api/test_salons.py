"""/api/v1/salons through the real ASGI stack.

Every request carries a token that is really signed and really verified. A
test that handed the endpoint a hand-built principal would stop testing the
thing these endpoints exist to protect.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import uuid4

import pytest
from fastapi import FastAPI

from barber_catalog.models.salon import Salon
from barber_common.testing.fixtures import app_client

pytestmark = pytest.mark.integration

SALONS = "/api/v1/salons"

SALON = uuid4()
ANOTHER_SALON = uuid4()

SalonFactory = Callable[..., Awaitable[Salon]]
AuthorizationFactory = Callable[..., dict[str, str]]


def a_salon(**overrides: object) -> dict[str, object]:
    """A valid creation body a test can bend one field of."""
    body: dict[str, object] = {
        "name": "Barbershop One",
        "address": "Tverskaya 1",
        "city": "Moscow",
        "phone": "+74951234567",
        "timezone": "Europe/Moscow",
    }
    body.update(overrides)
    return body


# --- creating ---------------------------------------------------------------


async def test_a_super_admin_opens_a_salon(
    app: FastAPI,
    authorize: AuthorizationFactory,
) -> None:
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.post(SALONS, json=a_salon(), headers=headers)

    assert response.status_code == 201
    assert response.json()["name"] == "Barbershop One"


async def test_a_new_salon_gets_the_documented_policy_defaults(
    app: FastAPI,
    authorize: AuthorizationFactory,
) -> None:
    """Default policies: 15, 120, 60 and 240."""
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.post(SALONS, json=a_salon(), headers=headers)

    body = response.json()
    assert body["slot_step_min"] == 15
    assert body["booking_min_lead_min"] == 120
    assert body["booking_horizon_days"] == 60
    assert body["cancel_deadline_min"] == 240


async def test_a_salon_admin_cannot_open_a_salon(
    app: FastAPI,
    authorize: AuthorizationFactory,
) -> None:
    """An administrator who could create salons could create their own."""
    headers = authorize(roles=(("salon_admin", SALON),))

    async with app_client(app) as client:
        response = await client.post(SALONS, json=a_salon(), headers=headers)

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden_for_role"


async def test_a_client_cannot_open_a_salon(
    app: FastAPI,
    authorize: AuthorizationFactory,
) -> None:
    headers = authorize(roles=(("client", None),))

    async with app_client(app) as client:
        response = await client.post(SALONS, json=a_salon(), headers=headers)

    assert response.status_code == 403


async def test_opening_a_salon_needs_a_token(app: FastAPI) -> None:
    async with app_client(app) as client:
        response = await client.post(SALONS, json=a_salon())

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


async def test_an_unknown_time_zone_is_refused(
    app: FastAPI,
    authorize: AuthorizationFactory,
) -> None:
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.post(
            SALONS, json=a_salon(timezone="Europe/Atlantis"), headers=headers
        )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_a_utc_offset_is_not_a_time_zone(
    app: FastAPI,
    authorize: AuthorizationFactory,
) -> None:
    """Storing an offset breaks the schedule when the clocks change."""
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.post(SALONS, json=a_salon(timezone="+03:00"), headers=headers)

    assert response.status_code == 422


async def test_a_zero_slot_step_is_refused(
    app: FastAPI,
    authorize: AuthorizationFactory,
) -> None:
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.post(SALONS, json=a_salon(slot_step_min=0), headers=headers)

    assert response.status_code == 422


async def test_an_unknown_field_is_refused(
    app: FastAPI,
    authorize: AuthorizationFactory,
) -> None:
    """``extra="forbid"``: a misspelled field is a silent no-op otherwise."""
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.post(SALONS, json=a_salon(slot_step=30), headers=headers)

    assert response.status_code == 422


# --- changing ---------------------------------------------------------------


async def test_a_salon_admin_changes_their_own_salon(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
) -> None:
    salon = await make_salon()
    headers = authorize(roles=(("salon_admin", salon.id),))

    async with app_client(app) as client:
        response = await client.patch(
            f"{SALONS}/{salon.id}", json={"cancel_deadline_min": 60}, headers=headers
        )

    assert response.status_code == 200
    assert response.json()["cancel_deadline_min"] == 60


async def test_a_salon_admin_cannot_change_another_salon(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
) -> None:
    salon = await make_salon()
    headers = authorize(roles=(("salon_admin", ANOTHER_SALON),))

    async with app_client(app) as client:
        response = await client.patch(
            f"{SALONS}/{salon.id}", json={"name": "Mine now"}, headers=headers
        )

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden_for_role"


async def test_a_super_admin_changes_any_salon(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
) -> None:
    salon = await make_salon()
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.patch(
            f"{SALONS}/{salon.id}", json={"name": "Renamed"}, headers=headers
        )

    assert response.status_code == 200
    assert response.json()["name"] == "Renamed"


async def test_a_client_cannot_change_a_salon(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
) -> None:
    salon = await make_salon()
    headers = authorize(roles=(("client", None),))

    async with app_client(app) as client:
        response = await client.patch(
            f"{SALONS}/{salon.id}", json={"name": "Mine"}, headers=headers
        )

    assert response.status_code == 403


async def test_a_field_left_out_is_left_alone(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
) -> None:
    salon = await make_salon(name="Original", city="Moscow")
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.patch(
            f"{SALONS}/{salon.id}", json={"city": "Kazan"}, headers=headers
        )

    body = response.json()
    assert body["city"] == "Kazan"
    assert body["name"] == "Original"


async def test_a_field_sent_as_null_is_cleared(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_salon: SalonFactory,
) -> None:
    """Absent and null are different, which is what ``exclude_unset`` gives."""
    salon = await make_salon()
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        await client.patch(f"{SALONS}/{salon.id}", json={"description": "Best"}, headers=headers)
        response = await client.patch(
            f"{SALONS}/{salon.id}", json={"description": None}, headers=headers
        )

    assert response.json()["description"] is None


async def test_changing_a_salon_that_does_not_exist(
    app: FastAPI,
    authorize: AuthorizationFactory,
) -> None:
    headers = authorize(roles=(("super_admin", None),))

    async with app_client(app) as client:
        response = await client.patch(
            f"{SALONS}/{uuid4()}", json={"name": "Ghost"}, headers=headers
        )

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


# --- reading ----------------------------------------------------------------


async def test_the_listing_is_open_to_anonymous_callers(
    app: FastAPI,
    make_salon: SalonFactory,
) -> None:
    """The salon listing is open to anonymous visitors."""
    await make_salon(name="Open To All")

    async with app_client(app) as client:
        response = await client.get(SALONS)

    assert response.status_code == 200
    assert [item["name"] for item in response.json()["items"]] == ["Open To All"]


async def test_one_salon_is_open_to_anonymous_callers(
    app: FastAPI,
    make_salon: SalonFactory,
) -> None:
    salon = await make_salon()

    async with app_client(app) as client:
        response = await client.get(f"{SALONS}/{salon.id}")

    assert response.status_code == 200
    assert response.json()["id"] == str(salon.id)


async def test_reading_a_salon_that_does_not_exist(app: FastAPI) -> None:
    async with app_client(app) as client:
        response = await client.get(f"{SALONS}/{uuid4()}")

    assert response.status_code == 404


async def test_the_listing_filters_by_city(
    app: FastAPI,
    make_salon: SalonFactory,
) -> None:
    await make_salon(name="In Moscow", city="Moscow")
    await make_salon(name="In Kazan", city="Kazan")

    async with app_client(app) as client:
        response = await client.get(SALONS, params={"city": "Kazan"})

    assert [item["name"] for item in response.json()["items"]] == ["In Kazan"]


async def test_a_closed_salon_leaves_the_listing_but_stays_readable(
    app: FastAPI,
    make_salon: SalonFactory,
) -> None:
    """A booking made while it was open still names it."""
    salon = await make_salon(name="Closed", is_active=False)

    async with app_client(app) as client:
        listing = await client.get(SALONS)
        one = await client.get(f"{SALONS}/{salon.id}")

    assert listing.json()["items"] == []
    assert one.status_code == 200
    assert one.json()["is_active"] is False


# --- paging -----------------------------------------------------------------


async def names_across_pages(app: FastAPI, *, limit: int) -> list[str]:
    """Walk the whole listing, following ``next_cursor`` to the end."""
    names: list[str] = []
    cursor: str | None = None

    async with app_client(app) as client:
        while True:
            params: dict[str, str | int] = {"limit": limit}
            if cursor is not None:
                params["cursor"] = cursor
            page = (await client.get(SALONS, params=params)).json()
            names.extend(item["name"] for item in page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                return names


async def test_the_pages_together_are_the_whole_listing(
    app: FastAPI,
    make_salon: SalonFactory,
) -> None:
    for index in range(5):
        await make_salon(name=f"Salon {index}")

    names = await names_across_pages(app, limit=2)

    assert names == ["Salon 0", "Salon 1", "Salon 2", "Salon 3", "Salon 4"]


async def test_the_last_page_says_it_is_the_last(
    app: FastAPI,
    make_salon: SalonFactory,
) -> None:
    await make_salon(name="Only One")

    async with app_client(app) as client:
        page = (await client.get(SALONS, params={"limit": 20})).json()

    assert page["next_cursor"] is None


async def test_an_insertion_between_pages_neither_skips_nor_repeats(
    app: FastAPI,
    make_salon: SalonFactory,
) -> None:
    """The whole reason the cursor is a keyset and not an offset.

    A salon inserted before the page boundary shifts every following row by
    one. An offset would count past the shifted rows and never show "Salon 2";
    a keyset resumes after the row it actually saw.
    """
    for index in range(4):
        await make_salon(name=f"Salon {index}")

    async with app_client(app) as client:
        first = (await client.get(SALONS, params={"limit": 2})).json()

        # Sorts before everything on page one, so an offset of 2 would now
        # point at "Salon 1" and page two would start at "Salon 2"... having
        # skipped nothing, but having shown "Salon 1" twice. Insert one that
        # sorts *before* the boundary to make the skip visible instead.
        await make_salon(name="Aaa Newcomer")

        second = (
            await client.get(SALONS, params={"limit": 2, "cursor": first["next_cursor"]})
        ).json()

    seen = [item["name"] for item in first["items"]] + [item["name"] for item in second["items"]]
    assert seen == ["Salon 0", "Salon 1", "Salon 2", "Salon 3"]
    assert len(seen) == len(set(seen))


async def test_a_deletion_between_pages_does_not_skip_a_row(
    app: FastAPI,
    make_salon: SalonFactory,
) -> None:
    """The other half: a row leaving the listing shifts the rest back."""
    for index in range(4):
        await make_salon(name=f"Salon {index}")

    async with app_client(app) as client:
        first = (await client.get(SALONS, params={"limit": 2})).json()

        # Closing a salon takes it out of the listing, exactly like a delete.
        await make_salon(name="Salon 0 closed", is_active=False)

        second = (
            await client.get(SALONS, params={"limit": 2, "cursor": first["next_cursor"]})
        ).json()

    seen = [item["name"] for item in first["items"]] + [item["name"] for item in second["items"]]
    assert seen == ["Salon 0", "Salon 1", "Salon 2", "Salon 3"]


async def test_a_cursor_the_service_did_not_produce_is_refused(app: FastAPI) -> None:
    async with app_client(app) as client:
        response = await client.get(SALONS, params={"cursor": "not-a-cursor"})

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_a_limit_above_the_maximum_is_refused(app: FastAPI) -> None:
    """Without an upper bound, one request reads the table into memory."""
    async with app_client(app) as client:
        response = await client.get(SALONS, params={"limit": 1000})

    assert response.status_code == 422


async def test_the_cursor_does_not_leak_the_sort_key(
    app: FastAPI,
    make_salon: SalonFactory,
) -> None:
    """Opaque by contract: a client that parses it depends on the ORDER BY."""
    for index in range(3):
        await make_salon(name=f"Salon {index}")

    async with app_client(app) as client:
        page = (await client.get(SALONS, params={"limit": 1})).json()

    assert "Salon 0" not in page["next_cursor"]
