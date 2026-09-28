"""Registration as one transaction: the user and its events, or neither."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from barber_auth.adapters.argon2_hasher import Argon2Hasher
from barber_auth.models.email_confirmation import EmailConfirmation
from barber_auth.models.user import User
from barber_auth.services.registration import RegisterUser
from barber_common.db.session import transaction
from barber_common.outbox.models import OutboxMessage

pytestmark = pytest.mark.integration

EMAIL = "ivan@example.com"
PHONE = "+79991234567"
PASSWORD = "correct-horse-9"


class RegistrationInterrupted(RuntimeError):
    """Stands in for anything that can fail after the events are queued."""


@pytest.fixture
def register(session: AsyncSession, hasher: Argon2Hasher) -> RegisterUser:
    return RegisterUser(
        session=session,
        hasher=hasher,
        confirmation_ttl_hours=24,
        password_min_length=10,
    )


async def count_of(session: AsyncSession, model: type[object]) -> int:
    result = await session.execute(select(func.count()).select_from(model))
    return int(result.scalar_one())


async def test_a_registration_writes_the_user_and_both_events_together(
    session: AsyncSession, register: RegisterUser
) -> None:
    await register.execute(email=EMAIL, phone=PHONE, password=PASSWORD)

    events = (await session.execute(select(OutboxMessage))).scalars().all()

    assert await count_of(session, User) == 1
    assert await count_of(session, EmailConfirmation) == 1
    assert {event.event_type for event in events} == {
        "user.registered",
        "user.email_confirmation_requested",
    }


async def test_a_transaction_that_rolls_back_leaves_no_event_behind(
    session: AsyncSession, register: RegisterUser
) -> None:
    # The scenario joins the transaction already open here instead of opening
    # one of its own, which is exactly what happens when a caller composes it
    # with more work -- and lets this test decide the outcome of the commit.
    with pytest.raises(RegistrationInterrupted):
        async with transaction(session):
            await register.execute(email=EMAIL, phone=PHONE, password=PASSWORD)
            raise RegistrationInterrupted

    # An event never outlives the state change that caused it.
    assert await count_of(session, OutboxMessage) == 0
    assert await count_of(session, User) == 0
    assert await count_of(session, EmailConfirmation) == 0


async def test_the_event_carries_the_normalised_contacts(
    session: AsyncSession, register: RegisterUser
) -> None:
    await register.execute(email="Ivan@Example.COM", phone="8 999 123 45 67", password=PASSWORD)

    statement = select(OutboxMessage).where(OutboxMessage.event_type == "user.registered")
    event = (await session.execute(statement)).scalar_one()

    assert event.payload["email"] == EMAIL
    assert event.payload["phone"] == PHONE
    assert event.payload["email_confirmed"] is False


async def test_the_event_goes_to_the_topic_of_the_user_aggregate(
    session: AsyncSession, register: RegisterUser
) -> None:
    registered = await register.execute(email=EMAIL, phone=PHONE, password=PASSWORD)

    events = (await session.execute(select(OutboxMessage))).scalars().all()

    # The key is the user: without it the partitioner spreads the events of one
    # account across partitions and "confirmed" can overtake "registered".
    assert {event.topic for event in events} == {"auth.users.v1"}
    assert {event.aggregate_id for event in events} == {registered.user_id}
    assert {event.aggregate_type for event in events} == {"users"}


async def test_the_password_is_stored_only_as_a_hash(
    session: AsyncSession, register: RegisterUser
) -> None:
    await register.execute(email=EMAIL, phone=PHONE, password=PASSWORD)

    user = (await session.execute(select(User))).scalar_one()

    assert PASSWORD not in user.password_hash
    assert user.password_hash.startswith("$argon2id$")


async def test_only_the_hash_of_the_confirmation_token_reaches_the_database(
    session: AsyncSession, register: RegisterUser
) -> None:
    await register.execute(email=EMAIL, phone=PHONE, password=PASSWORD)

    statement = select(OutboxMessage).where(
        OutboxMessage.event_type == "user.email_confirmation_requested"
    )
    event = (await session.execute(statement)).scalar_one()
    confirmation = (await session.execute(select(EmailConfirmation))).scalar_one()

    token = event.payload["token"]
    assert isinstance(token, str)
    assert token not in confirmation.token_hash
    # sha256, hex encoded: the row is a fingerprint of the token, not the token.
    assert len(confirmation.token_hash) == 64
