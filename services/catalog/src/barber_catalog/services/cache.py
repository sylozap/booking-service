"""What the catalog caches, and what makes a cached answer stop counting.

Three reads are cached, and they are the three the platform actually leans on:
the master card a visitor opens, one service, and the internal answer
``booking`` reads before every availability calculation. The listings are not
cached -- each page has its own cursor and its own filter, so each is its own
key, and a cache of one-visit keys is a cache that only evicts.

**Invalidation is a generation counter, not a set of deletes.** Every key
carries the generation it was written under, and a write anywhere in the
catalog increments the counter, which makes every key written under the old one
unreachable at once. One ``INCR`` invalidates everything, there is nothing to
enumerate and nothing to scan, and -- the reason this scheme was chosen -- a
key cannot be forgotten. Deleting exactly the affected keys would be more
precise and would require every write path to know which reads it touches: an
edit to one service changes the card of every master offering it and every
internal answer that mentions it, and the day someone adds a fourth cached read
without revisiting all eleven write scenarios, the cache starts lying.

**The counter is one for the whole catalog rather than one per salon.** A key
has to be computable from what the request carries, and
``GET /api/v1/masters/{id}`` does not carry a salon -- finding it would take
the database query the cache exists to avoid. The price is that a write in one
salon invalidates the reads of all of them. With twenty salons
(docs/03-services.md), writes that are administrative operations, and a
five-minute expiry underneath, that costs a handful of extra database reads a
day.

**Invalidation happens after the commit.** Redis and PostgreSQL cannot be made
atomic with each other, and holding a database connection open across a network
call to Redis is forbidden outright (docs/CODING_STANDARDS.md section 8). What
remains is the race every cache-aside scheme has: a reader that queried before
a write commits can store what it read after the invalidation, and that entry
stays until it expires. The TTL is the bound on it, which is why it is five
minutes and not five hours.
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

        The document is validated against the model rather than trusted. A
        release that changes the shape of a response leaves documents written
        by the previous one in Redis, and handing one of those to a caller
        would turn a deployment into a stream of malformed answers. A document
        that no longer parses is treated as a miss and quietly replaced.
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
        """The namespace the current generation writes and reads under.

        A second round trip to Redis before the value itself. It buys the
        property that makes this scheme worth having: invalidation is one
        command that cannot miss a key. Against the database query it replaces
        -- a card is three statements with their joins -- two Redis reads are
        not a cost worth optimising away.
        """
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
