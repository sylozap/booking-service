"""Linking a Telegram chat over HTTP: who may, and what comes back."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import uuid4

import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from barber_common.testing.fixtures import app_client
from barber_notification.models.recipient import Recipient
from barber_notification.models.telegram_link_code import TelegramLinkCode
from barber_notification.repositories.recipients import RecipientRepository
from barber_notification.services.telegram_link import hash_code

pytestmark = pytest.mark.integration

AuthorizationFactory = Callable[..., dict[str, str]]
RecipientFactory = Callable[..., Awaitable[Recipient]]

LINK_CODE = "/api/v1/notifications/telegram/link-code"
LINK = "/api/v1/notifications/telegram/link"


async def test_a_signed_in_user_gets_a_code_and_a_link_to_the_bot(
    telegram_app: FastAPI, authorize: AuthorizationFactory, session: AsyncSession
) -> None:
    user_id = uuid4()

    async with app_client(telegram_app) as client:
        response = await client.post(LINK_CODE, headers=authorize(user_id=user_id))

    assert response.status_code == 201
    body = response.json()
    assert body["link"] == f"https://t.me/barber_bot?start={body['code']}"
    stored = (await session.execute(select(TelegramLinkCode))).scalars().all()
    [row] = [code for code in stored if code.user_id == user_id]
    # Only the hash is kept: the table alone links nobody's chat.
    assert row.code_hash == hash_code(body["code"])
    assert row.code_hash != body["code"]


async def test_a_code_is_refused_without_a_token(telegram_app: FastAPI) -> None:
    async with app_client(telegram_app) as client:
        response = await client.post(LINK_CODE)

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


async def test_without_a_bot_there_is_no_code_to_give(
    app: FastAPI, authorize: AuthorizationFactory
) -> None:
    async with app_client(app) as client:
        response = await client.post(LINK_CODE, headers=authorize())

    assert response.status_code == 422
    assert response.json()["code"] == "channel_unavailable"


async def test_unlinking_forgets_the_chat(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_recipient: RecipientFactory,
    session: AsyncSession,
) -> None:
    recipient = await make_recipient(telegram_chat_id=42)

    async with app_client(app) as client:
        response = await client.delete(LINK, headers=authorize(user_id=recipient.user_id))

    assert response.status_code == 204
    after = await RecipientRepository(session).get(recipient.user_id)
    assert after is not None
    assert after.telegram_chat_id is None


async def test_unlinking_touches_nobody_else(
    app: FastAPI,
    authorize: AuthorizationFactory,
    make_recipient: RecipientFactory,
    session: AsyncSession,
) -> None:
    other = await make_recipient(telegram_chat_id=42)

    async with app_client(app) as client:
        await client.delete(LINK, headers=authorize())

    after = await RecipientRepository(session).get(other.user_id)
    assert after is not None
    assert after.telegram_chat_id == 42


async def test_unlinking_without_a_chat_is_safe_to_repeat(
    app: FastAPI, authorize: AuthorizationFactory
) -> None:
    async with app_client(app) as client:
        first = await client.delete(LINK, headers=authorize())
        second = await client.delete(LINK, headers=authorize())

    assert (first.status_code, second.status_code) == (204, 204)


async def test_unlinking_is_refused_without_a_token(app: FastAPI) -> None:
    async with app_client(app) as client:
        response = await client.delete(LINK)

    assert response.status_code == 401
