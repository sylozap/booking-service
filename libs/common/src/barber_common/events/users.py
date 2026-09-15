"""Payloads of ``auth.users.v1``.

Shared by ``auth``, which writes them, and ``notification``, which reads them.
The topic carries the account lifecycle, from which ``notification`` keeps its
own table of recipients.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict

__all__ = [
    "AUTH_USERS_TOPIC",
    "USER_AGGREGATE_TYPE",
    "UserContactsUpdated",
    "UserDeactivated",
    "UserEmailConfirmationRequested",
    "UserEmailConfirmed",
    "UserEventType",
    "UserRegistered",
]

AUTH_USERS_TOPIC = "auth.users.v1"

# The partitioning key of every event here, and the aggregate they belong to.
USER_AGGREGATE_TYPE = "users"


class UserEventType(StrEnum):
    """``event_type`` of the envelope, for the producer and the consumer alike.

    A consumer logs and commits an event type it does not recognise.
    """

    REGISTERED = "user.registered"
    EMAIL_CONFIRMATION_REQUESTED = "user.email_confirmation_requested"
    EMAIL_CONFIRMED = "user.email_confirmed"
    CONTACTS_UPDATED = "user.contacts_updated"
    DEACTIVATED = "user.deactivated"


class _UserEvent(BaseModel):
    """What every payload of this topic carries.

    Frozen, because an event describes something that has already happened, and
    tolerant of unknown fields, because a consumer running an older schema must
    not stop a partition over a field it does not know.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    user_id: UUID


class UserRegistered(_UserEvent):
    """An account was created. ``notification`` learns the contacts here."""

    email: str
    phone: str
    email_confirmed: bool = False


class UserEmailConfirmationRequested(_UserEvent):
    """A confirmation link was issued and has to reach the address.

    The payload carries the token, which is a credential and is never logged.
    """

    email: str
    token: str
    expires_at: str


class UserEmailConfirmed(_UserEvent):
    """The address was confirmed and may now be used to reach the user."""

    email: str


class UserContactsUpdated(_UserEvent):
    """The user changed an email or a phone number."""

    email: str
    phone: str
    email_confirmed: bool


class UserDeactivated(_UserEvent):
    """The account is no longer active. Nothing is sent to it."""
