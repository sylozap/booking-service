"""Registration and email confirmation.

Both endpoints are deliberately anonymous: they are how an account comes into
existence, and there is no role to check because there is no caller yet. Rate
limiting is what protects them, and it lives on the gateway (T6.3): 3 emails
per hour per address, by docs/04-api-contracts.md.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from barber_auth.api.v1.dependencies import ConfirmEmailScenario, RegisterUserScenario
from barber_auth.schemas.auth import (
    ConfirmEmailRequest,
    ConfirmEmailResponse,
    RegisterRequest,
    RegisterResponse,
)

__all__ = ["router"]

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    summary="Register a client account",
    responses={
        status.HTTP_409_CONFLICT: {"description": "Email or phone is already registered"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Contacts or password rejected"},
    },
)
async def register(body: RegisterRequest, scenario: RegisterUserScenario) -> RegisterResponse:
    """Create an account and send a confirmation letter.

    **A duplicate answers 409 with a domain code**, which tells the caller that
    the address exists. The alternative -- always answering 201 and saying
    nothing -- hides the account from an attacker enumerating addresses, at the
    price of a user who cannot tell "registered" from "already registered" and
    waits for a letter that will not arrive. The platform is a booking service
    for barbershops, not a system where account existence is itself sensitive,
    so convenience wins. Written down here because reversing it later is a
    decision, not a fix.
    """
    registered = await scenario.execute(
        email=body.email,
        phone=body.phone,
        password=body.password,
    )
    return RegisterResponse(
        user_id=registered.user_id,
        email=registered.email,
        phone=registered.phone,
        role=registered.role.value,
    )


@router.post(
    "/confirm-email",
    status_code=status.HTTP_200_OK,
    summary="Confirm an email address with a one-time token",
    responses={
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Token is unknown, already used or expired"
        }
    },
)
async def confirm_email(
    body: ConfirmEmailRequest, scenario: ConfirmEmailScenario
) -> ConfirmEmailResponse:
    """Spend the token from the confirmation link.

    Unknown, spent and expired tokens all answer with one code: separate
    answers would tell a caller which tokens exist.
    """
    confirmed = await scenario.execute(token=body.token)
    return ConfirmEmailResponse(user_id=confirmed.user_id, email=confirmed.email)
