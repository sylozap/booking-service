"""What booking caches, and what makes a cached answer stop counting.

Every key carries the generations of the master and of the service it
describes. An event about either increments its generation, and every key
written under the old one becomes unreachable at once: one ``INCR``, nothing to
enumerate, nothing to scan. ``service.updated`` names no master, so a delete of
exact keys would first have to find every master offering the service; the
service's own counter needs no such search.

Keys live under ``booking:`` so they never meet the keys of ``catalog`` in a
shared Redis. What is left behind under an old generation expires by TTL.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ValidationError

from barber_booking.domain.identifiers import MasterId, ServiceId
from barber_common.cache import Cache
from barber_common.logging import get_logger

__all__ = ["KEY_PREFIX", "BookingCache"]

_logger = get_logger(__name__)

KEY_PREFIX = "booking"

# Read when a counter was never set or Redis did not answer: the cache then
# behaves as an empty one.
FIRST_GENERATION = 0


class BookingCache:
    """The key scheme of booking, over a cache that may be missing."""

    def __init__(self, cache: Cache) -> None:
        self._cache = cache

    async def read[ModelT: BaseModel](self, key: str, model: type[ModelT]) -> ModelT | None:
        """Return a cached document, or nothing.

        A document that no longer parses -- written by a release with another
        schema -- is a miss.
        """
        raw = await self._cache.get(key)
        if raw is None:
            return None

        try:
            return model.model_validate_json(raw)
        except ValidationError:
            _logger.warning("cached document no longer matches its schema", cache_key=key)
            return None

    async def write(self, key: str, document: BaseModel, *, ttl_seconds: int | None = None) -> None:
        """Store a document under the key it was read for."""
        await self._cache.set(
            key, document.model_dump_json().encode("utf-8"), ttl_seconds=ttl_seconds
        )

    async def offering_key(self, master_id: MasterId, service_id: ServiceId) -> str:
        """Key of the answer of ``catalog`` about one master and one service."""
        master = await self._generation(_master_generation_key(master_id))
        service = await self._generation(_service_generation_key(service_id))
        return f"{KEY_PREFIX}:offering:{master_id}:{service_id}:{master}.{service}"

    async def invalidate_master(self, master_id: MasterId) -> None:
        """Make every key about this master unreachable."""
        await self._cache.increment(_master_generation_key(master_id))

    async def invalidate_service(self, service_id: ServiceId) -> None:
        """Make every key about this service unreachable, for every master."""
        await self._cache.increment(_service_generation_key(service_id))

    async def _generation(self, key: str) -> int:
        raw = await self._cache.get(key)
        if raw is None:
            return FIRST_GENERATION
        try:
            return int(raw)
        except ValueError:
            _logger.warning("cache generation is not a number", cache_key=key)
            return FIRST_GENERATION


def _master_generation_key(master_id: UUID) -> str:
    return f"{KEY_PREFIX}:generation:master:{master_id}"


def _service_generation_key(service_id: UUID) -> str:
    return f"{KEY_PREFIX}:generation:service:{service_id}"
