"""Database constraints of the catalog schema, on a real PostgreSQL."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from barber_catalog.models.master import Master
from barber_catalog.models.master_service import MasterService
from barber_catalog.models.salon import Salon
from barber_catalog.models.service import Service
from barber_common.db.errors import SQLSTATE_CHECK_VIOLATION, SQLSTATE_UNIQUE_VIOLATION, sqlstate_of

pytestmark = pytest.mark.integration

SalonFactory = Callable[..., Awaitable[Salon]]
MasterFactory = Callable[..., Awaitable[Master]]
ServiceFactory = Callable[..., Awaitable[Service]]
OfferingFactory = Callable[..., Awaitable[MasterService]]


async def test_a_service_of_zero_minutes_is_refused_by_the_database(
    session: AsyncSession,
    make_salon: SalonFactory,
) -> None:
    salon = await make_salon()

    session.add(
        Service(
            salon_id=salon.id,
            name="Instant haircut",
            base_duration_min=0,
            base_price=Decimal("100.00"),
            currency="RUB",
        )
    )
    with pytest.raises(IntegrityError) as failure:
        await session.flush()

    assert sqlstate_of(failure.value) == SQLSTATE_CHECK_VIOLATION


async def test_one_account_holds_one_profile_per_salon(
    session: AsyncSession,
    make_salon: SalonFactory,
    make_master: MasterFactory,
) -> None:
    salon = await make_salon()
    user_id = uuid4()
    await make_master(salon_id=salon.id, user_id=user_id)

    session.add(Master(salon_id=salon.id, user_id=user_id, display_name="Ivan again"))
    with pytest.raises(IntegrityError) as failure:
        await session.flush()

    assert sqlstate_of(failure.value) == SQLSTATE_UNIQUE_VIOLATION


async def test_one_account_may_be_a_master_in_two_salons(
    make_salon: SalonFactory,
    make_master: MasterFactory,
) -> None:
    first = await make_salon(name="One")
    second = await make_salon(name="Two")
    user_id = uuid4()

    await make_master(salon_id=first.id, user_id=user_id)
    profile = await make_master(salon_id=second.id, user_id=user_id)

    assert profile.salon_id == second.id


def test_an_unknown_time_zone_is_refused_by_the_model() -> None:
    """The model refuses an unknown time zone when the object is built."""
    with pytest.raises(ValueError, match="not a known IANA time zone"):
        Salon(
            name="Nowhere",
            address="Nowhere 1",
            city="Nowhere",
            phone="+70000000000",
            timezone="Europe/Atlantis",
        )


def test_an_offset_is_not_a_time_zone() -> None:
    """A salon keeps a zone name, never ``+03:00``."""
    with pytest.raises(ValueError, match="not a known IANA time zone"):
        Salon(
            name="Offset",
            address="Offset 1",
            city="Offset",
            phone="+70000000000",
            timezone="+03:00",
        )


async def test_a_known_time_zone_is_accepted(make_salon: SalonFactory) -> None:
    salon = await make_salon(timezone="Asia/Yekaterinburg")

    assert salon.timezone == "Asia/Yekaterinburg"


async def test_a_salon_cannot_have_a_zero_slot_step(
    session: AsyncSession,
    make_salon: SalonFactory,
) -> None:
    salon = await make_salon()

    salon.slot_step_min = 0
    with pytest.raises(IntegrityError) as failure:
        await session.flush()

    assert sqlstate_of(failure.value) == SQLSTATE_CHECK_VIOLATION


async def test_a_salon_gets_the_documented_policy_defaults(session: AsyncSession) -> None:
    """Default policies set by the database: 15, 120, 60 and 240."""
    salon = Salon(
        name="Defaults",
        address="Default 1",
        city="Moscow",
        phone="+74950000000",
        timezone="Europe/Moscow",
    )
    session.add(salon)
    await session.flush()
    await session.refresh(salon)

    assert salon.slot_step_min == 15
    assert salon.booking_min_lead_min == 120
    assert salon.booking_horizon_days == 60
    assert salon.cancel_deadline_min == 240


async def test_a_master_offers_a_service_only_once(
    session: AsyncSession,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
    make_offering: OfferingFactory,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(salon_id=salon.id)
    await make_offering(master_id=master.id, service_id=service.id)

    session.add(MasterService(master_id=master.id, service_id=service.id))
    with pytest.raises(IntegrityError) as failure:
        await session.flush()

    assert sqlstate_of(failure.value) == SQLSTATE_UNIQUE_VIOLATION


async def test_a_duration_override_of_zero_is_refused(
    session: AsyncSession,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
) -> None:
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(salon_id=salon.id)

    session.add(MasterService(master_id=master.id, service_id=service.id, duration_override=0))
    with pytest.raises(IntegrityError) as failure:
        await session.flush()

    assert sqlstate_of(failure.value) == SQLSTATE_CHECK_VIOLATION


async def test_deleting_a_master_takes_their_offerings_with_them(
    session: AsyncSession,
    make_salon: SalonFactory,
    make_master: MasterFactory,
    make_service: ServiceFactory,
    make_offering: OfferingFactory,
) -> None:
    """The one cascade this schema has, and the one it deliberately lacks.

    A profile owns its offerings, so removing it removes them. A service does
    not: it is never deleted, and the foreign key says so by having no cascade.
    """
    salon = await make_salon()
    master = await make_master(salon_id=salon.id)
    service = await make_service(salon_id=salon.id)
    await make_offering(master_id=master.id, service_id=service.id)

    await session.delete(master)
    await session.flush()

    assert await session.get(MasterService, (master.id, service.id)) is None
    assert await session.get(Service, service.id) is not None
