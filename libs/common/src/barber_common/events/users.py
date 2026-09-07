"""Payloads of ``auth.users.v1``.

One source of truth for ``auth``, which writes them, and ``notification``,
which reads them: a payload that stops matching is caught by mypy on both sides
rather than in a consumer log (ADR-0005).

The topic carries the account lifecycle and nothing else. ``notification``
keeps its own table of recipients and fills it from these events, so a
notification still goes out while ``auth`` is unavailable
(docs/03-services.md).

These are schemas, not domain types: the fields are the ones on the wire, the
identifiers are plain ``UUID``, and no rule of the auth service leaks into the
chassis (ADR-0016).
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

    A consumer that does not recognise a member logs it and commits: an unknown
    event type never stops a partition (docs/07-events-and-kafka.md).
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

    The token travels in the payload. It is a credential, so three things hold
    it in check: the topic is internal to the platform, the token expires
    within a day, and the database keeps only its hash. It is never written to
    a log record -- the only place it is allowed to appear is the letter
    (docs/07-events-and-kafka.md).
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
