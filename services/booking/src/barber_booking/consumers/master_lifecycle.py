"""The life cycle of masters, as booking follows it from ``catalog.masters.v1``.

A consumer group of its own, apart from the one retiring cached data. The two
fail for different reasons -- a database here, Redis there -- and one must not
hold the other back. Starting from the earliest offset, a new group also reads
the ``master.created`` events published before it existed, so masters made
earlier get their settings without a manual step.

Each handler runs in the runner's transaction, together with the row that marks
the event processed: a redelivered event is dropped before it gets here.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from barber_booking.domain.identifiers import MasterId
from barber_booking.services.cascade_cancel import CancelMasterBookings
from barber_booking.services.master_lifecycle import FollowMaster
from barber_common.events.catalog import (
    CATALOG_MASTERS_TOPIC,
    MasterCreated,
    MasterDeactivated,
    MasterEventType,
    MasterUpdated,
)
from barber_common.events.envelope import JsonEnvelope
from barber_common.kafka import EventHandler

__all__ = ["MASTER_LIFECYCLE_GROUP", "MASTER_LIFECYCLE_TOPICS", "MasterLifecycle"]

MASTER_LIFECYCLE_GROUP = "booking.masters"
MASTER_LIFECYCLE_TOPICS = (CATALOG_MASTERS_TOPIC,)


class MasterLifecycle:
    """The handlers that keep master settings in step with catalog."""

    def handlers(self) -> dict[str, EventHandler]:
        """Event types this group reacts to, and how."""
        return {
            MasterEventType.CREATED: self.master_created,
            MasterEventType.UPDATED: self.master_updated,
            MasterEventType.DEACTIVATED: self.master_deactivated,
        }

    async def master_created(self, session: AsyncSession, envelope: JsonEnvelope) -> None:
        await FollowMaster(session).created(MasterCreated.model_validate(envelope.payload))

    async def master_updated(self, session: AsyncSession, envelope: JsonEnvelope) -> None:
        """Covers reactivation too: it is announced as an update with is_active."""
        await FollowMaster(session).updated(MasterUpdated.model_validate(envelope.payload))

    async def master_deactivated(self, session: AsyncSession, envelope: JsonEnvelope) -> None:
        """The cascade: every visit of the master still ahead is cancelled."""
        event = MasterDeactivated.model_validate(envelope.payload)
        await CancelMasterBookings(session).execute(
            master_id=MasterId(event.master_id), cause=envelope.event_id
        )
