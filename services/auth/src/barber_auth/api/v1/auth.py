"""Registration, email confirmation, and the lifecycle of a session.

Every endpoint here is anonymous, and each for its own reason. Registration and
confirmation are how an account comes into existence, so there is no caller to
check a role on. Login, refresh and logout are how a caller *becomes* one: the
credential is in the body, and it is what is being verified.

That makes the rate limits on the gateway (T6.3) the protection of this module
rather than an addition to it -- three confirmation letters per hour per
address, and the anonymous per-IP limit on the rest
(docs/04-api-contracts.md).
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

    **A wrong password, an unknown address and a deactivated account answer
    identically**, with `401` and the same body, and take comparable time to
    do it. Anything else would let a caller enumerate the accounts of the
    platform by watching which answers differ.

    **An unconfirmed address is the one exception** and answers `403` with
    `email_not_confirmed`. The caller has already proven they know the
    password at that point, so naming the reason reveals nothing they could
    not learn anyway, and without it a user who never clicked the link has no
    way to find out why they cannot get in.

    The refresh token is returned in the body; where a client stores it is
    outside the reach of this service.
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

    Every rotation invalidates the token it was given. **Presenting a token
    that was already exchanged revokes the entire family it belongs to** --
    including the pair issued a moment earlier -- because two parties holding
    one token means one of them took a copy, and there is no way to tell from
    here which of the two is calling.

    The answer to that case is the same `401` an expired token gets: telling
    the caller that the reuse was noticed would only inform whoever stole it.
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

    With `all_devices` false this ends the session the token belongs to and
    leaves the others alone; with it true, every session of that user ends.

    **Access tokens already issued keep working until they expire.** They are
    verified locally against a public key, with nothing consulted that could be
    told to stop honouring them, and that is the trade-off recorded in
    [ADR-0010](docs/adr/0010-jwt-verified-in-services.md): the compensation is
    the fifteen-minute TTL and the fact that nothing can be renewed after this
    call. A logout is therefore complete within fifteen minutes, not instantly.

    Always `204`, including for a token that is unknown or already revoked:
    logging out is safe to repeat, and an endpoint that answered differently
    for a token that exists would be a way to test whether one does.
    """
    await scenario.execute(refresh_token=body.refresh_token, all_devices=body.all_devices)
