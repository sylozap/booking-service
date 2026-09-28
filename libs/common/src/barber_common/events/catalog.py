"""Payloads of the topics ``catalog`` publishes.

Shared by ``catalog``, which writes them, and ``booking``, which reads them.
``catalog.masters.v1`` carries the life cycle of master profiles and
``catalog.services.v1`` that of salon services. Each topic is keyed by the id
of its aggregate, so the events of one aggregate stay in order.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict

__all__ = [
    "CATALOG_MASTERS_TOPIC",
    "CATALOG_SERVICES_TOPIC",
    "MASTER_AGGREGATE_TYPE",
    "SERVICE_AGGREGATE_TYPE",
    "MasterCreated",
    "MasterDeactivated",
    "MasterEventType",
    "MasterUpdated",
    "ServiceArchived",
    "ServiceCreated",
    "ServiceEventType",
    "ServiceUpdated",
]

CATALOG_MASTERS_TOPIC = "catalog.masters.v1"
CATALOG_SERVICES_TOPIC = "catalog.services.v1"

# The aggregate of each topic; its id is the partitioning key. Masters and
# services use separate topics so the events of one cannot overtake the other.
MASTER_AGGREGATE_TYPE = "masters"
SERVICE_AGGREGATE_TYPE = "services"


class MasterEventType(StrEnum):
    """``event_type`` of the envelope, for producer and consumer alike.

    A consumer logs and commits an event type it does not recognise.
    """

    CREATED = "master.created"
    UPDATED = "master.updated"
    DEACTIVATED = "master.deactivated"


class _MasterEvent(BaseModel):
    """What every payload of this topic carries.

    Frozen, because an event describes something that has already happened, and
    tolerant of unknown fields, because a consumer running an older schema must
    not stop a partition over a field it does not know.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    master_id: UUID
    salon_id: UUID


class MasterCreated(_MasterEvent):
    """A master profile was created; ``booking`` creates its settings row.

    ``timezone`` is the zone of the salon, included so ``booking`` handles the
    event without calling ``catalog``. ``user_id`` is not verified against
    ``auth``.
    """

    user_id: UUID
    display_name: str
    timezone: str
    is_active: bool


class MasterDeactivated(_MasterEvent):
    """A master was deactivated; ``booking`` cancels their future bookings.

    Carries only the identity. Reactivating the master does not restore the
    cancelled bookings.
    """


class MasterUpdated(_MasterEvent):
    """The current state of a master after a change.

    A full snapshot keeps consumers idempotent under at-least-once delivery.
    Offered services are not included. ``timezone`` is the zone of the salon:
    a change of it is announced as this event for every master of the salon,
    because the schedule of each is written in that zone. It is optional so a
    snapshot written before it existed still parses.
    """

    user_id: UUID
    display_name: str
    specialization: str | None
    is_active: bool
    timezone: str | None = None


class _ServiceEvent(BaseModel):
    """What every payload of ``catalog.services.v1`` carries."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    service_id: UUID
    salon_id: UUID


class ServiceEventType(StrEnum):
    """``event_type`` of the envelope, for producer and consumer alike."""

    CREATED = "service.created"
    UPDATED = "service.updated"
    ARCHIVED = "service.archived"


class ServiceCreated(_ServiceEvent):
    """A salon added a service to its price list."""

    name: str
    base_duration_min: int
    base_price: Decimal
    currency: str


class ServiceUpdated(_ServiceEvent):
    """A service changed. ``booking`` drops what it cached about it.

    The same snapshot shape as :class:`ServiceCreated`. ``base_price`` is the
    salon's listed price, before any master override.
    """

    name: str
    base_duration_min: int
    base_price: Decimal
    currency: str


class ServiceArchived(_ServiceEvent):
    """A salon withdrew a service. It is not deleted and never will be.

    Bookings that already name it stay readable, so a consumer must not treat
    this as a signal to forget the service -- only as one to stop offering it.
    """
