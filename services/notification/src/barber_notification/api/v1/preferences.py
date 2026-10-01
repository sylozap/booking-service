"""The caller's notification switches.

Any signed-in user, for their own account only: the account is the subject of
the token, never a parameter.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from barber_common.auth.dependencies import CurrentUser
from barber_notification.api.v1.dependencies import (
    ReadPreferencesScenario,
    UpdatePreferencesScenario,
)
from barber_notification.schemas.preferences import PreferencesPatch, PreferencesResponse

__all__ = ["router"]

router = APIRouter(prefix="/notifications/preferences", tags=["notifications"])


@router.get(
    "",
    summary="Read which notifications go to which channels",
    responses={status.HTTP_401_UNAUTHORIZED: {"description": "No valid user token"}},
)
async def read_preferences(
    caller: CurrentUser, scenario: ReadPreferencesScenario
) -> PreferencesResponse:
    """Every switch is on until turned off."""
    return PreferencesResponse.of(await scenario.execute(caller.user_id))


@router.patch(
    "",
    summary="Switch kinds of notification on or off per channel",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "No valid user token"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "`validation_error`: an unknown kind, channel or field"
        },
    },
)
async def update_preferences(
    body: PreferencesPatch, caller: CurrentUser, scenario: UpdatePreferencesScenario
) -> PreferencesResponse:
    """Only the switches named change; the answer is every switch after the change.

    Turning everything off is allowed. The confirmation letter of an email
    address is sent whatever the switches say.
    """
    return PreferencesResponse.of(await scenario.execute(caller.user_id, body.changes()))
