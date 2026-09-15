"""Cached reads of the catalog and their invalidation.

Cached: the master card, one service, and the internal answer for ``booking``.
Listings are not cached.

Invalidation uses one generation counter for the whole catalog: every key
includes the current generation, and each write increments it after the
commit. Stale entries left by a concurrent read are bounded by the TTL.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ValidationError

from barber_common.cache import Cache
from barber_common.logging import get_logger

__all__ = ["GENERATION_KEY", "KEY_PREFIX", "CatalogCache"]

_logger = get_logger(__name__)

KEY_PREFIX = "catalog"
GENERATION_KEY = f"{KEY_PREFIX}:generation"

# The generation to read under when the counter has never been set, or when
# Redis did not answer. Zero rather than a failure: an unreadable counter makes
# the cache behave as one that is simply empty.
FIRST_GENERATION = 0


class CatalogCache:
    """The key scheme of the catalog, over a cache that may be missing."""

    def __init__(self, cache: Cache) -> None:
        self._cache = cache

    async def read[ModelT: BaseModel](self, key: str, model: type[ModelT]) -> ModelT | None:
        """Return a cached document, or nothing.

        A document that no longer matches the model is treated as a miss.
        """
        raw = await self._cache.get(key)
        if raw is None:
            return None

        try:
            return model.model_validate_json(raw)
        except ValidationError:
            _logger.warning("cached document no longer matches its schema", cache_key=key)
            return None

    async def write(self, key: str, document: BaseModel) -> None:
        """Store a document under the key it was read for."""
        await self._cache.set(key, document.model_dump_json().encode("utf-8"))

    async def invalidate(self) -> None:
        """Make every key written so far unreachable.

        Called by each write scenario **after** its transaction has committed.
        One command, whatever changed and however much of it.
        """
        await self._cache.increment(GENERATION_KEY)

    async def master_card_key(self, master_id: UUID) -> str:
        """Key of ``GET /api/v1/masters/{id}``."""
        return f"{await self._prefix()}:master-card:{master_id}"

    async def service_key(self, service_id: UUID) -> str:
        """Key of ``GET /api/v1/services/{id}``."""
        return f"{await self._prefix()}:service:{service_id}"

    async def offering_key(self, master_id: UUID, service_id: UUID) -> str:
        """Key of ``GET /internal/v1/masters/{id}/services/{sid}``."""
        return f"{await self._prefix()}:offering:{master_id}:{service_id}"

    async def _prefix(self) -> str:
        """The key prefix of the current generation."""
        return f"{KEY_PREFIX}:{await self._generation()}"

    async def _generation(self) -> int:
        """The current generation, or the first one if it cannot be read.

        An unreadable or nonsensical counter makes every key resolve to
        generation zero. That is a cache that behaves as though it were empty,
        which is the only safe way for this to fail.
        """
        raw = await self._cache.get(GENERATION_KEY)
        if raw is None:
            return FIRST_GENERATION

        try:
            return int(raw)
        except ValueError:
            _logger.warning("cache generation is not a number", cache_key=GENERATION_KEY)
            return FIRST_GENERATION
