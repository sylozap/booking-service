"""Schemas of the asynchronous messages of the platform.

One source of truth for the producer and the consumer: a payload that stops
matching is caught by mypy on both sides rather than at three in the morning
in a consumer log.
"""

from barber_common.events.catalog import (
    CATALOG_MASTERS_TOPIC,
    MASTER_AGGREGATE_TYPE,
    MasterCreated,
    MasterDeactivated,
    MasterEventType,
)
from barber_common.events.envelope import (
    ENVELOPE_VERSION,
    EventEnvelope,
    JsonEnvelope,
    build_envelope,
)
from barber_common.events.users import (
    AUTH_USERS_TOPIC,
    USER_AGGREGATE_TYPE,
    UserContactsUpdated,
    UserDeactivated,
    UserEmailConfirmationRequested,
    UserEmailConfirmed,
    UserEventType,
    UserRegistered,
)

__all__ = [
    "AUTH_USERS_TOPIC",
    "CATALOG_MASTERS_TOPIC",
    "ENVELOPE_VERSION",
    "MASTER_AGGREGATE_TYPE",
    "USER_AGGREGATE_TYPE",
    "EventEnvelope",
    "JsonEnvelope",
    "MasterCreated",
    "MasterDeactivated",
    "MasterEventType",
    "UserContactsUpdated",
    "UserDeactivated",
    "UserEmailConfirmationRequested",
    "UserEmailConfirmed",
    "UserEventType",
    "UserRegistered",
    "build_envelope",
]
