"""Reading what a master offers, through the cache.

The hot path: availability asks this before every calculation. A miss, or a
cache that is down, goes to ``catalog`` and answers the same way.
"""

from __future__ import annotations

from barber_booking.clients.catalog import CatalogClient
from barber_booking.domain.identifiers import MasterId, ServiceId
from barber_booking.services.cache import BookingCache
from barber_common.contracts.catalog import MasterServiceDetails

__all__ = ["ReadOffering"]


class ReadOffering:
    """One master offering one service, as ``catalog`` last described it."""

    def __init__(self, catalog: CatalogClient, cache: BookingCache) -> None:
        self._catalog = catalog
        self._cache = cache

    async def execute(self, *, master_id: MasterId, service_id: ServiceId) -> MasterServiceDetails:
        """Answer from the cache, or ask ``catalog`` and remember the answer.

        The key is computed before the call. An invalidation that lands while
        ``catalog`` is being asked moves the generation, so the stale answer is
        stored under a key nobody reads any more.
        """
        key = await self._cache.offering_key(master_id, service_id)
        cached = await self._cache.read(key, MasterServiceDetails)
        if cached is not None:
            return cached

        details = await self._catalog.get_offering(master_id=master_id, service_id=service_id)
        await self._cache.write(key, details)
        return details
