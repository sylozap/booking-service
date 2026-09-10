"""Payloads of the topics ``catalog`` publishes.

One source of truth for ``catalog``, which writes them, and ``booking``, which
reads them: a payload that stops matching is caught by mypy on both sides
rather than in a consumer log (ADR-0005).

``catalog.masters.v1`` carries the life cycle of a master profile. ``booking``
consumes it to create the ``master_settings`` row a schedule hangs off, and
later to cascade cancellations when a master is deactivated
(docs/03-services.md). The partitioning key is the master, so the events of one
master stay in order -- a ``master.deactivated`` overtaking the
``master.created`` that has not been handled yet would cascade over bookings
nobody can attach to anything.

These are schemas, not domain types: the fields are the ones on the wire, the
identifiers are plain ``UUID``, and no rule of the catalog leaks into the
chassis (ADR-0016).
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

# The partitioning key of the events on each topic, and the aggregate they
# belong to. Two topics rather than one, because ordering is guaranteed within
# a partition and a partition is chosen by the key: mixing masters and services
# on one topic would let a service.archived overtake the master.updated that
# was published first (docs/07-events-and-kafka.md).
MASTER_AGGREGATE_TYPE = "masters"
SERVICE_AGGREGATE_TYPE = "services"


class MasterEventType(StrEnum):
    """``event_type`` of the envelope, for producer and consumer alike.

    A consumer that does not recognise a member logs it and commits: an unknown
    event type never stops a partition (docs/07-events-and-kafka.md).
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
    """A master profile exists. ``booking`` gives it a settings row.

    ``timezone`` travels with the event on purpose. It belongs to the salon,
    and ``booking`` needs it the moment it creates ``master_settings`` -- a
    weekly schedule is written in the salon's local time, and a settings row
    without a zone cannot be unfolded into instants. Sending it here saves
    ``booking`` a synchronous call to ``catalog`` on the handling path of every
    new master, which is exactly the coupling events exist to remove.

    ``user_id`` is the account in ``auth`` behind the profile. It is not
    verified anywhere: a profile naming an account that does not exist is a
    data error, not a broken system (T2.3).
    """

    user_id: UUID
    display_name: str
    timezone: str
    is_active: bool


class MasterDeactivated(_MasterEvent):
    """A master has stopped working. Their future bookings are cancelled.

    **This is the one event in the platform that changes another service's
    state as a cascade** (docs/03-services.md): ``booking`` consumes it and
    cancels every future booking of this master with
    ``cancelled_by_salon``. That is why the endpoint answers ``202`` -- the
    cancellations have not happened yet when the caller is told the master is
    deactivated.

    It carries nothing but the identity. A cascade needs to know **who**, and
    every other fact about the master is either already in ``booking`` or
    irrelevant to cancelling an appointment. A payload that carried the profile
    would invite a consumer to treat this as an update as well, and the two
    have very different consequences.

    **Reactivation does not undo it.** There is no event for that and there
    could not be a useful one: the bookings were cancelled, the clients were
    told, and the slots have been open to everyone else since.
    """


class MasterUpdated(_MasterEvent):
    """Something about a master changed, and here is the whole of it now.

    A snapshot rather than a set of changed fields, for two reasons. A consumer
    applying a snapshot is idempotent for free -- the same event delivered
    twice leaves the same state -- which matters because delivery is
    at-least-once (ADR-0007). And a consumer that joined late, or replayed from
    the start of the topic, reaches the right state from any single event
    rather than needing every one that came before.

    ``services`` is deliberately not here. What a master offers is published by
    this event happening at all, not by its contents: a consumer that needs the
    prices asks the internal endpoint, which is the one place that resolves
    ``COALESCE(override, base)``. Putting a price list in an event would give
    the platform a second answer to that question.

    ``timezone`` is not here either, although :class:`MasterCreated` carries
    it. The zone belongs to the salon and cannot change through an edit of a
    master, so repeating it would mean loading the salon on every profile
    change to send a value the consumer already has.
    """

    user_id: UUID
    display_name: str
    specialization: str | None
    is_active: bool


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

    The same snapshot shape as :class:`ServiceCreated`, so a consumer that
    treats the two alike is correct rather than lucky.

    ``base_price`` is what the salon lists, not what any master charges. The
    figure a booking is written with comes from the internal endpoint, which
    resolves the master's override against this.
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
