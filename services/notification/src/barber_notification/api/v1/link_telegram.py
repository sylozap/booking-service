"""Linking the caller's Telegram chat.

Any signed-in user, for their own account only: the account is the subject of
the token, never a parameter.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from barber_common.auth.dependencies import CurrentUser
from barber_notification.api.v1.dependencies import (
    IssueTelegramLinkCodeScenario,
    UnlinkTelegramScenario,
)
from barber_notification.schemas.telegram import TelegramLinkCodeResponse

__all__ = ["router"]

router = APIRouter(prefix="/notifications/telegram", tags=["notifications"])


@router.post(
    "/link-code",
    status_code=status.HTTP_201_CREATED,
    summary="Get a one-time code for linking a Telegram chat",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "No valid user token"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "`channel_unavailable`: Telegram is not configured on this platform"
        },
    },
)
async def issue_link_code(
    caller: CurrentUser, scenario: IssueTelegramLinkCodeScenario
) -> TelegramLinkCodeResponse:
    """Open the returned link and press Start: the chat becomes your Telegram address.

    The code is single use and expires; asking again issues a new one without
    spoiling the old. Linking a new chat replaces the previous one.
    """
    issued = await scenario.execute(caller.user_id)
    return TelegramLinkCodeResponse(
        code=issued.code, link=issued.link, expires_at=issued.expires_at
    )


@router.delete(
    "/link",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Stop sending notifications to Telegram",
    responses={status.HTTP_401_UNAUTHORIZED: {"description": "No valid user token"}},
)
async def unlink(caller: CurrentUser, scenario: UnlinkTelegramScenario) -> None:
    """Forget the linked chat. Safe to repeat: without a chat it changes nothing."""
    await scenario.execute(caller.user_id)
