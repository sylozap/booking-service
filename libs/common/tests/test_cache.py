"""A cache whose failures are misses.

Half of these are about Redis working and half about it not. The second half is
the point: the cache decides nothing (ADR-0012), so every way it can fail has
to come out as "nothing found" rather than as a failed request.
"""

from __future__ import annotations

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from barber_common.cache import Cache, cache_from_dsn

pytestmark = pytest.mark.integration


@pytest.fixture
async def cache(redis_dsn: str) -> Cache:
    """A cache over the Redis of this session."""
    return Cache(cache_from_dsn(redis_dsn, timeout_seconds=1.0), ttl_seconds=60)


@pytest.fixture
def unreachable_cache() -> Cache:
    """A cache pointed at a port nothing is listening on.

    Port 1 rather than a made-up hostname: a name that does not resolve fails
    differently on different machines, while a closed port refuses the
    connection everywhere.
    """
    return Cache(cache_from_dsn("redis://127.0.0.1:1/0", timeout_seconds=0.05), ttl_seconds=60)


async def test_a_stored_value_comes_back(cache: Cache) -> None:
    await cache.set("test:round-trip", b"stored")

    assert await cache.get("test:round-trip") == b"stored"


async def test_a_key_that_was_never_written_is_a_miss(cache: Cache) -> None:
    assert await cache.get("test:never-written") is None


async def test_a_stored_value_expires(cache: Cache, redis_dsn: str) -> None:
    """Every write carries a TTL; a key without one outlives every release."""
    await cache.set("test:expiring", b"stored", ttl_seconds=42)

    client: Redis = cache_from_dsn(redis_dsn, timeout_seconds=1.0)
    try:
        assert 0 < await client.ttl("test:expiring") <= 42
    finally:
        await client.aclose()


async def test_a_counter_starts_at_one_and_climbs(cache: Cache) -> None:
    """What generation-based invalidation is built on."""
    key = "test:generation"
    await cache.set(key, b"0")

    first = await cache.increment(key)
    second = await cache.increment(key)

    assert (first, second) == (1, 2)


async def test_an_unreachable_cache_reads_as_a_miss(unreachable_cache: Cache) -> None:
    """The property the whole service leans on: a read falls through."""
    assert await unreachable_cache.get("test:anything") is None


async def test_an_unreachable_cache_swallows_a_write(unreachable_cache: Cache) -> None:
    await unreachable_cache.set("test:anything", b"stored")


async def test_an_unreachable_cache_swallows_an_increment(unreachable_cache: Cache) -> None:
    """Invalidation against a dead cache is a no-op, not a failed request."""
    assert await unreachable_cache.increment("test:generation") is None


async def test_an_unreachable_cache_closes_quietly(unreachable_cache: Cache) -> None:
    """A cache that cannot be closed must not stop a pod from exiting."""
    await unreachable_cache.aclose()


async def test_a_disabled_cache_stores_nothing(cache: Cache) -> None:
    disabled = Cache.disabled()

    await disabled.set("test:disabled", b"stored")

    assert await disabled.get("test:disabled") is None
    assert await cache.get("test:disabled") is None


async def test_a_disabled_cache_says_so() -> None:
    assert Cache.disabled().is_enabled is False


async def test_an_enabled_cache_says_so(cache: Cache) -> None:
    assert cache.is_enabled is True


async def test_a_disabled_cache_increments_nothing() -> None:
    assert await Cache.disabled().increment("test:generation") is None


async def test_a_disabled_cache_closes_quietly() -> None:
    await Cache.disabled().aclose()


@pytest.mark.parametrize("failure", [RedisConnectionError, RedisTimeoutError, OSError])
async def test_every_kind_of_failure_reads_as_a_miss(
    cache: Cache,
    failure: type[BaseException],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout is not a RedisError in redis-py, and a socket error is neither.

    Catching only RedisError would let the slow case -- the one that matters
    most, because a slow cache is worse than a missing one -- escape into the
    request.
    """

    async def fail(_key: str) -> bytes:
        raise failure("cache is having a bad day")

    monkeypatch.setattr(cache, "_client", _ClientThatFails(fail))

    assert await cache.get("test:anything") is None


class _ClientThatFails:
    """Stands in for the Redis client, not for anything the project owns.

    Mocking is limited to external boundaries (docs/CODING_STANDARDS.md
    section 14), and the Redis driver is one: there is no way to ask a real
    server to produce a socket error on demand.
    """

    def __init__(self, on_get: object) -> None:
        self.get = on_get
