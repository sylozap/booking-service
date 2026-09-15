"""Registration, email confirmation, and the lifecycle of a session.

Every endpoint here is anonymous: the credential being verified is in the
request body.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from barber_auth.api.v1.dependencies import (
    ConfirmEmailScenario,
    IssueTokenPairScenario,
    RefreshTokenPairScenario,
    RegisterUserScenario,
    RequestSessionContext,
    RevokeSessionsScenario,
)
from barber_auth.schemas.auth import (
    ConfirmEmailRequest,
    ConfirmEmailResponse,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    RegisterResponse,
    TokenPairResponse,
)
from barber_auth.services.tokens import TokenPair

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

    A duplicate email or phone answers `409` with a domain code.
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


def _token_pair_response(pair: TokenPair) -> TokenPairResponse:
    """One place where a minted pair becomes a body.

    Login and refresh return the same thing, and the day the shape changes it
    should change once.
    """
    return TokenPairResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        access_expires_at=pair.access_expires_at,
        refresh_expires_at=pair.refresh_expires_at,
    )


@router.post(
    "/login",
    status_code=status.HTTP_200_OK,
    summary="Exchange credentials for an access and a refresh token",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Email or password is not correct"},
        status.HTTP_403_FORBIDDEN: {"description": "Email address is not confirmed"},
    },
)
async def login(
    body: LoginRequest,
    scenario: IssueTokenPairScenario,
    context: RequestSessionContext,
) -> TokenPairResponse:
    """Sign in.

    A wrong password, an unknown address and a deactivated account all answer
    `401` with the same body. An unconfirmed address answers `403` with
    `email_not_confirmed`.

    The refresh token is returned in the body.
    """
    pair = await scenario.execute(
        email=body.email,
        password=body.password,
        context=context,
    )
    return _token_pair_response(pair)


@router.post(
    "/refresh",
    status_code=status.HTTP_200_OK,
    summary="Rotate a refresh token into a new pair",
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Refresh token is unknown, expired, or already spent"
        }
    },
)
async def refresh(
    body: RefreshRequest,
    scenario: RefreshTokenPairScenario,
    context: RequestSessionContext,
) -> TokenPairResponse:
    """Renew a session.

    Every rotation invalidates the token it was given. Presenting a token that
    was already exchanged revokes its entire family and answers `401`.
    """
    pair = await scenario.execute(refresh_token=body.refresh_token, context=context)
    return _token_pair_response(pair)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="End the current session, or every session of the user",
)
async def logout(body: LogoutRequest, scenario: RevokeSessionsScenario) -> None:
    """Sign out.

    With `all_devices` false this ends the session the token belongs to; with
    it true, every session of the user ends. Access tokens already issued keep
    working until they expire, within fifteen minutes.

    Always answers `204`, including for an unknown or already revoked token.
    """
    await scenario.execute(refresh_token=body.refresh_token, all_devices=body.all_devices)
