"""Bodies of ``/api/v1/auth``."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from barber_auth.domain.passwords import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH

__all__ = ["ConfirmEmailRequest", "ConfirmEmailResponse", "RegisterRequest", "RegisterResponse"]


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
