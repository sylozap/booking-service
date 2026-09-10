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

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict

__all__ = [
    "CATALOG_MASTERS_TOPIC",
    "MASTER_AGGREGATE_TYPE",
    "MasterCreated",
    "MasterDeactivated",
    "MasterEventType",
]

CATALOG_MASTERS_TOPIC = "catalog.masters.v1"

# The partitioning key of every event here, and the aggregate they belong to.
MASTER_AGGREGATE_TYPE = "masters"


class MasterEventType(StrEnum):
    """``event_type`` of the envelope, for producer and consumer alike.

    A consumer that does not recognise a member logs it and commits: an unknown
    event type never stops a partition (docs/07-events-and-kafka.md). The
    remaining member of this topic, ``master.updated``, arrives with T2.9.
    """

    CREATED = "master.created"
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
