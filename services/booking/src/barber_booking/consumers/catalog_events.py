"""Forgetting what booking cached about the catalog, as soon as it changes.

A changed master or service increments its generation in the cache, which
retires both the cached answers of ``catalog`` and the cached availability that
depend on it. A duplicate delivery only increments once more; an event type
without a handler here is logged and committed by the runner.

The Redis call happens inside the transaction the runner opens to record the
event. It is bounded by the cache timeout and touches no row, so it holds the
connection for milliseconds, and a Redis that is down is a miss rather than a
failure: the TTL then bounds how long the stale answer lives.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from barber_booking.domain.identifiers import MasterId, ServiceId
from barber_booking.services.cache import BookingCache
from barber_common.events.catalog import (
    CATALOG_MASTERS_TOPIC,
    CATALOG_SERVICES_TOPIC,
    MasterDeactivated,
    MasterEventType,
    MasterUpdated,
    ServiceArchived,
    ServiceEventType,
    ServiceUpdated,
)
from barber_common.events.envelope import JsonEnvelope
from barber_common.kafka import EventHandler

__all__ = ["CATALOG_TOPICS", "CONSUMER_GROUP", "CatalogCacheInvalidation"]

CONSUMER_GROUP = "booking.catalog"
CATALOG_TOPICS = (CATALOG_MASTERS_TOPIC, CATALOG_SERVICES_TOPIC)


class CatalogCacheInvalidation:
    """The handlers that retire cached catalog data."""

    def __init__(self, cache: BookingCache) -> None:
        self._cache = cache

    def handlers(self) -> dict[str, EventHandler]:
        """Event types this group reacts to, and how."""
        return {
            MasterEventType.UPDATED: self.master_updated,
            MasterEventType.DEACTIVATED: self.master_deactivated,
            ServiceEventType.UPDATED: self.service_updated,
            ServiceEventType.ARCHIVED: self.service_archived,
        }

    async def master_updated(self, _session: AsyncSession, envelope: JsonEnvelope) -> None:
        """Covers a new price or duration too: linking a service publishes this."""
        event = MasterUpdated.model_validate(envelope.payload)
        await self._cache.invalidate_master(MasterId(event.master_id))

    async def master_deactivated(self, _session: AsyncSession, envelope: JsonEnvelope) -> None:
        """Without this the cached answer keeps saying the master is active."""
        event = MasterDeactivated.model_validate(envelope.payload)
        await self._cache.invalidate_master(MasterId(event.master_id))

    async def service_updated(self, _session: AsyncSession, envelope: JsonEnvelope) -> None:
        """A new base duration changes every master who does not override it."""
        event = ServiceUpdated.model_validate(envelope.payload)
        await self._cache.invalidate_service(ServiceId(event.service_id))

    async def service_archived(self, _session: AsyncSession, envelope: JsonEnvelope) -> None:
        event = ServiceArchived.model_validate(envelope.payload)
        await self._cache.invalidate_service(ServiceId(event.service_id))
