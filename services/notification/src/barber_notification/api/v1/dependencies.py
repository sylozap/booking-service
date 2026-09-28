"""Assembling the scenarios a request needs.

A scenario takes its dependencies through the constructor, so it can be built
in a test without an application behind it. Here they are put together from
the request's session and the settings of the running service.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from barber_common.db.session import get_session
from barber_notification.services.telegram_link import IssueTelegramLinkCode, UnlinkTelegram
from barber_notification.settings import NotificationSettings

__all__ = [
    "IssueTelegramLinkCodeScenario",
    "SessionDependency",
    "UnlinkTelegramScenario",
]

SessionDependency = Annotated[AsyncSession, Depends(get_session)]


def get_settings(request: Request) -> NotificationSettings:
    settings = request.app.state.settings
    if not isinstance(settings, NotificationSettings):  # pragma: no cover - set by create_app
        raise RuntimeError("application state has no notification settings")
    return settings


SettingsDependency = Annotated[NotificationSettings, Depends(get_settings)]


def build_issue_telegram_link_code(
    session: SessionDependency, settings: SettingsDependency
) -> IssueTelegramLinkCode:
    return IssueTelegramLinkCode(
        session,
        # Without a token nobody polls the bot, and a code would link nothing.
        bot_username=settings.telegram_bot_username if settings.telegram_enabled else None,
        ttl=timedelta(minutes=settings.telegram_link_code_ttl_minutes),
    )


def build_unlink_telegram(session: SessionDependency) -> UnlinkTelegram:
    return UnlinkTelegram(session)


IssueTelegramLinkCodeScenario = Annotated[
    IssueTelegramLinkCode, Depends(build_issue_telegram_link_code)
]
UnlinkTelegramScenario = Annotated[UnlinkTelegram, Depends(build_unlink_telegram)]
