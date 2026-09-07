"""Bodies of ``/api/v1/auth``."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from barber_auth.domain.passwords import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH

__all__ = [
    "ConfirmEmailRequest",
    "ConfirmEmailResponse",
    "LoginRequest",
    "LogoutRequest",
    "RefreshRequest",
    "RegisterRequest",
    "RegisterResponse",
    "TokenPairResponse",
]

# Bounds of the opaque refresh token in a request body. 32 random bytes become
# 43 base64url characters; the window is wide enough for the value to change
# shape later and narrow enough that nothing large reaches the hash function.
REFRESH_TOKEN_MIN_LENGTH = 32
REFRESH_TOKEN_MAX_LENGTH = 512


class RegisterRequest(BaseModel):
    """What a new client sends.

    ``phone`` is a string in any human spelling; normalising it to E.164 is a
    rule and lives in the domain, not in an annotation, because the same rule
    applies to the writes that never pass through this schema.
    """

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    phone: str = Field(min_length=5, max_length=32)
    # Bounds only. The policy -- letters, digits, no surrounding whitespace --
    # is checked in the domain so that the message names the rule that failed.
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)


class RegisterResponse(BaseModel):
    """What the caller learns about the account it just created.

    No password hash, no confirmation token, no internal columns.
    """

    user_id: UUID
    email: str
    phone: str
    role: str
    email_confirmed: bool = False


class ConfirmEmailRequest(BaseModel):
    """The one-time token taken from the link in the letter."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=16, max_length=256)


class ConfirmEmailResponse(BaseModel):
    """The account whose address is now confirmed."""

    user_id: UUID
    email: str
    email_confirmed: bool = True


class LoginRequest(BaseModel):
    """Credentials of an existing account."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    # No policy bounds here, unlike registration. The policy applies to the
    # password being set, not to the one being checked: tightening it later
    # must not lock out the accounts created under the older rule.
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class RefreshRequest(BaseModel):
    """The refresh token being exchanged for a new pair."""

    model_config = ConfigDict(extra="forbid")

    refresh_token: str = Field(
        min_length=REFRESH_TOKEN_MIN_LENGTH,
        max_length=REFRESH_TOKEN_MAX_LENGTH,
    )


class LogoutRequest(BaseModel):
    """Which session to end, and whether to end all of them."""

    model_config = ConfigDict(extra="forbid")

    refresh_token: str = Field(
        min_length=REFRESH_TOKEN_MIN_LENGTH,
        max_length=REFRESH_TOKEN_MAX_LENGTH,
    )
    all_devices: bool = Field(
        default=False,
        description=(
            "Revoke every session of this user rather than only the one the token belongs to."
        ),
    )


class TokenPairResponse(BaseModel):
    """What a login or a rotation returns.

    **The refresh token is in the body.** Where a client keeps it afterwards is
    outside the reach of this service, and pretending otherwise by putting it
    in a cookie would only move the decision somewhere less visible; the
    trade-off is stated here so that it is part of the published contract
    rather than folklore.

    Both expiry moments are returned so a client can schedule a refresh instead
    of discovering the expiry through a ``401``. They are RFC 3339 in UTC, like
    every other timestamp in the API.
    """

    access_token: str
    refresh_token: str
    # noqa: S105 below -- "Bearer" is the scheme name RFC 6750 requires in the
    # Authorization header, not a secret.
    token_type: str = "Bearer"  # noqa: S105
    access_expires_at: datetime
    refresh_expires_at: datetime
