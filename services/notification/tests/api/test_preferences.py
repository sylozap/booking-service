"""The notification switches over HTTP: whose they are, and what they change."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import uuid4

import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from barber_common.events.bookings import BOOKINGS_TOPIC
from barber_common.testing.fixtures import app_client
from barber_notification.models.notification import Notification
from barber_notification.models.recipient import Recipient
from barber_notification.preferences import NotificationKind
from barber_notification.repositories.recipients import RecipientRepository
from barber_notification.services.dispatch import EnqueueNotification

pytestmark = pytest.mark.integration

AuthorizationFactory = Callable[..., dict[str, str]]
RecipientFactory = Callable[..., Awaitable[Recipient]]

PREFERENCES = "/api/v1/notifications/preferences"

EVERYTHING_ON = {
    "bookings": {"email": True, "telegram": True},
    "reminders": {"email": True, "telegram": True},
}


async def test_a_user_this_service_has_not_heard_of_reads_everything_on(
    app: FastAPI, authorize: AuthorizationFactory
) -> None:
    async with app_client(app) as client:
        response = await client.get(PREFERENCES, headers=authorize())

    assert response.status_code == 200
    assert response.json() == EVERYTHING_ON


async def test_a_change_flips_only_the_switches_it_names(
    app: FastAPI, authorize: AuthorizationFactory, make_recipient: RecipientFactory
) -> None:
    recipient = await make_recipient(preferences={"bookings": {"email": False}})
    headers = authorize(user_id=recipient.user_id)

    async with app_client(app) as client:
        changed = await client.patch(
            PREFERENCES, json={"reminders": {"telegram": False}}, headers=headers
        )
        read = await client.get(PREFERENCES, headers=headers)

    assert changed.status_code == 200
    assert changed.json() == {
        "bookings": {"email": False, "telegram": True},
        "reminders": {"email": True, "telegram": False},
    }
    assert read.json() == changed.json()


async def test_every_switch_may_be_turned_off(
    app: FastAPI, authorize: AuthorizationFactory
) -> None:
    everything_off = {
        "bookings": {"email": False, "telegram": False},
        "reminders": {"email": False, "telegram": False},
    }

    async with app_client(app) as client:
        response = await client.patch(PREFERENCES, json=everything_off, headers=authorize())

    assert response.status_code == 200
    assert response.json() == everything_off


async def test_a_change_before_the_account_events_creates_the_recipient(
    app: FastAPI, authorize: AuthorizationFactory, session: AsyncSession
) -> None:
    user_id = uuid4()

    async with app_client(app) as client:
        await client.patch(
            PREFERENCES, json={"bookings": {"telegram": False}}, headers=authorize(user_id=user_id)
        )

    recipient = await RecipientRepository(session).get(user_id)
    assert recipient is not None
    assert recipient.preferences["bookings"] == {"email": True, "telegram": False}


async def test_a_change_touches_nobody_else(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_recipient: RecipientFactory,
    session: AsyncSession,
) -> None:
    other = await make_recipient()

    async with app_client(app) as client:
        await client.patch(PREFERENCES, json={"bookings": {"email": False}}, headers=authorize())

    untouched = await RecipientRepository(session).get(other.user_id)
    assert untouched is not None
    assert untouched.preferences == {}


async def test_someone_elses_switches_cannot_be_named(
    app: FastAPI, authorize: AuthorizationFactory, make_recipient: RecipientFactory
) -> None:
    other = await make_recipient()
    body = {"user_id": str(other.user_id), "bookings": {"email": False}}

    async with app_client(app) as client:
        response = await client.patch(PREFERENCES, json=body, headers=authorize())

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


@pytest.mark.parametrize(
    "body",
    [
        {"marketing": {"email": False}},
        {"bookings": {"sms": False}},
        {"bookings": {"email": "no"}},
    ],
    ids=["unknown kind", "unknown channel", "not a boolean"],
)
async def test_a_switch_that_does_not_exist_is_refused(
    app: FastAPI, authorize: AuthorizationFactory, body: dict[str, object]
) -> None:
    async with app_client(app) as client:
        response = await client.patch(PREFERENCES, json=body, headers=authorize())

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


@pytest.mark.parametrize("method", ["GET", "PATCH"])
async def test_the_switches_need_a_token(app: FastAPI, method: str) -> None:
    async with app_client(app) as client:
        response = await client.request(method, PREFERENCES, json={})

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


async def test_a_channel_switched_off_is_not_used_for_that_kind(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_recipient: RecipientFactory,
    session: AsyncSession,
) -> None:
    recipient = await make_recipient(telegram_chat_id=42)
    async with app_client(app) as client:
        await client.patch(
            PREFERENCES,
            json={"bookings": {"telegram": False}},
            headers=authorize(user_id=recipient.user_id),
        )

    await EnqueueNotification(session).execute(
        event_id=uuid4(),
        topic=BOOKINGS_TOPIC,
        user_id=recipient.user_id,
        kind=NotificationKind.BOOKINGS,
        template="booking_created",
        fields={"service_name": "Стрижка", "start_at": "05.09.2026 15:00"},
    )

    statement = select(Notification.channel).where(Notification.user_id == recipient.user_id)
    assert list((await session.execute(statement)).scalars()) == ["email"]
