"""POST /api/v1/bookings on behalf of another client, by the salon."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, time, timedelta
from typing import Protocol
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from barber_booking.models.booking import Booking
from barber_booking.models.master_settings import MasterSettings
from barber_common.events.bookings import BookingEventType
from barber_common.outbox.models import OutboxMessage
from barber_common.testing.fixtures import app_client

pytestmark = pytest.mark.integration


class CatalogStub(Protocol):
    """The part of the fake catalog these tests read."""

    salon_id: UUID
    calls: int


MasterSettingsFactory = Callable[..., Awaitable[MasterSettings]]
TemplateFactory = Callable[..., Awaitable[object]]
AuthorizationFactory = Callable[..., dict[str, str]]

# A month ahead, ten in Moscow: clear of every window of the salon.
WORKDAY = datetime.now(UTC).date() + timedelta(days=30)
TEN_MOSCOW = datetime.combine(WORKDAY, time(7), tzinfo=UTC)
SERVICE_ID = uuid4()


async def working_master(
    make_master_settings: MasterSettingsFactory, make_template: TemplateFactory
) -> MasterSettings:
    master = await make_master_settings()
    await make_template(master_id=master.master_id, weekday=WORKDAY.weekday())
    return master


async def book_for(
    app: FastAPI,
    master: MasterSettings,
    client_user_id: UUID | None,
    headers: dict[str, str],
    *,
    start_at: datetime = TEN_MOSCOW,
    key: str | None = None,
) -> httpx.Response:
    body: dict[str, object] = {
        "master_id": str(master.master_id),
        "service_id": str(SERVICE_ID),
        "start_at": start_at.isoformat(),
    }
    if client_user_id is not None:
        body["client_user_id"] = str(client_user_id)
    async with app_client(app) as client:
        return await client.post(
            "/api/v1/bookings",
            json=body,
            headers={**headers, "Idempotency-Key": key or str(uuid4())},
        )


async def bookings_of(session: AsyncSession, master_id: UUID) -> int:
    statement = select(func.count()).select_from(Booking).where(Booking.master_id == master_id)
    return int((await session.execute(statement)).scalar_one())


# --- the salon books a client -----------------------------------------------


async def test_an_admin_of_the_salon_books_for_another_user(
    app: FastAPI,
    catalog: CatalogStub,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await working_master(make_master_settings, make_template)
    admin_id, client_id = uuid4(), uuid4()

    response = await book_for(
        app,
        master,
        client_id,
        authorize(roles=(("salon_admin", catalog.salon_id),), user_id=admin_id),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["client_user_id"] == str(client_id)
    assert body["created_by"] == str(admin_id)


async def test_the_event_names_both_the_client_and_who_booked_them(
    app: FastAPI,
    session: AsyncSession,
    catalog: CatalogStub,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await working_master(make_master_settings, make_template)
    admin_id, client_id = uuid4(), uuid4()

    await book_for(
        app,
        master,
        client_id,
        authorize(roles=(("salon_admin", catalog.salon_id),), user_id=admin_id),
    )

    statement = select(OutboxMessage).where(
        OutboxMessage.event_type == BookingEventType.CREATED.value
    )
    [event] = (await session.execute(statement)).scalars().all()
    assert event.payload["client_user_id"] == str(client_id)
    assert event.payload["created_by"] == str(admin_id)


async def test_a_super_admin_books_for_another_user(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await working_master(make_master_settings, make_template)

    response = await book_for(app, master, uuid4(), authorize(roles=(("super_admin", None),)))

    assert response.status_code == 201


async def test_a_booking_of_ones_own_names_the_client_as_its_maker(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await working_master(make_master_settings, make_template)
    client_id = uuid4()

    response = await book_for(app, master, None, authorize(user_id=client_id))

    assert response.json()["created_by"] == str(client_id)


async def test_a_client_may_name_themselves(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await working_master(make_master_settings, make_template)
    client_id = uuid4()

    response = await book_for(app, master, client_id, authorize(user_id=client_id))

    assert response.status_code == 201


async def test_the_salon_is_held_to_the_same_windows_as_a_client(
    app: FastAPI,
    catalog: CatalogStub,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await working_master(make_master_settings, make_template)

    response = await book_for(
        app,
        master,
        uuid4(),
        authorize(roles=(("salon_admin", catalog.salon_id),)),
        start_at=datetime.now(UTC) + timedelta(minutes=30),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "booking_too_late"


async def test_a_repeated_request_of_the_admin_answers_the_same_booking(
    app: FastAPI,
    session: AsyncSession,
    catalog: CatalogStub,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await working_master(make_master_settings, make_template)
    headers = authorize(roles=(("salon_admin", catalog.salon_id),))
    client_id, key = uuid4(), str(uuid4())

    first = await book_for(app, master, client_id, headers, key=key)
    second = await book_for(app, master, client_id, headers, key=key)

    assert second.json() == first.json()
    assert await bookings_of(session, master.master_id) == 1


# --- who may not ------------------------------------------------------------


async def test_a_client_naming_somebody_else_is_refused_before_catalog_is_asked(
    app: FastAPI,
    session: AsyncSession,
    catalog: CatalogStub,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await working_master(make_master_settings, make_template)
    calls = catalog.calls

    response = await book_for(app, master, uuid4(), authorize())

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden_for_role"
    assert catalog.calls == calls
    assert await bookings_of(session, master.master_id) == 0


async def test_an_admin_of_another_salon_is_refused(
    app: FastAPI,
    session: AsyncSession,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await working_master(make_master_settings, make_template)

    response = await book_for(app, master, uuid4(), authorize(roles=(("salon_admin", uuid4()),)))

    assert response.status_code == 403
    assert await bookings_of(session, master.master_id) == 0


async def test_an_admin_without_the_client_role_books_only_for_others(
    app: FastAPI,
    catalog: CatalogStub,
    authorize: AuthorizationFactory,
    make_master_settings: MasterSettingsFactory,
    make_template: TemplateFactory,
) -> None:
    master = await working_master(make_master_settings, make_template)

    response = await book_for(
        app, master, None, authorize(roles=(("salon_admin", catalog.salon_id),))
    )

    assert response.status_code == 403
