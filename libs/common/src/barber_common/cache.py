"""Redis read-through cache the service can run without.

* Any Redis error is logged and treated as a miss.
* Every call runs under a timeout.
* A Redis that keeps failing is left alone for a while: a circuit breaker
  turns every call into an immediate miss instead of a timeout each.
* :meth:`Cache.disabled` builds a cache with no client, for deployments without
  Redis and for tests.

Key schemes and invalidation belong to the service that uses the cache.
"""

from __future__ import annotations

from redis.asyncio import Redis
from redis.exceptions import RedisError

from barber_common.http.circuit_breaker import CircuitBreaker, CircuitState
from barber_common.logging import get_logger
from barber_common.metrics import counter

__all__ = ["Cache", "CacheOutcome", "cache_from_dsn"]

_logger = get_logger(__name__)

# Cache operations by outcome; the "error" outcome separates a failing Redis
# from a falling hit ratio.
CACHE_OPERATIONS = counter(
    "cache_operations_total",
    "Cache reads and writes by outcome",
    labelnames=("operation", "outcome"),
)


class CacheOutcome:
    """Label values of :data:`CACHE_OPERATIONS`.

    Named rather than spelled inline: a typo in a label value is a metric that
    silently splits into two series.
    """

    HIT = "hit"
    MISS = "miss"
    STORED = "stored"
    ERROR = "error"
    # Not attempted: the breaker is open and Redis is being left alone.
    BYPASSED = "bypassed"


# Errors a cache is allowed to fail with. RedisError covers the protocol and
# the connection; OSError covers a socket that never got that far; TimeoutError
# is what a deadline produces and, in redis-py, is not always a RedisError.
_FAILURES = (RedisError, OSError, TimeoutError)

# Three failures in a row open the breaker, and Redis is tried again after five
# seconds. Without it a stopped Redis cost every operation its whole timeout:
# an availability query makes four, and at the peak load booking and catalog
# spent their CPU and their database connections waiting on a cache.
BREAKER_FAILURE_THRESHOLD = 3
BREAKER_RESET_TIMEOUT_SECONDS = 5.0


def cache_breaker() -> CircuitBreaker:
    """The breaker a :class:`Cache` uses unless it is given one."""
    return CircuitBreaker(
        name="redis",
        failure_threshold=BREAKER_FAILURE_THRESHOLD,
        reset_timeout_seconds=BREAKER_RESET_TIMEOUT_SECONDS,
    )


def cache_from_dsn(
    dsn: str,
    *,
    timeout_seconds: float,
) -> Redis:
    """Build the Redis client a :class:`Cache` wraps.

    ``decode_responses`` stays off: the values stored here are JSON documents
    that the caller parses itself, and decoding them to ``str`` first only to
    encode them again is work for nothing.
    """
    return Redis.from_url(
        dsn,
        socket_connect_timeout=timeout_seconds,
        socket_timeout=timeout_seconds,
        decode_responses=False,
    )


class Cache:
    """A cache whose failures are misses.

    Values are bytes. Serialisation belongs to the caller, which knows the
    schema and can validate what comes back -- a document written by an older
    release must not be handed to a model that has changed shape.
    """

    def __init__(
        self,
        client: Redis | None,
        *,
        ttl_seconds: int,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._client = client
        self._ttl_seconds = ttl_seconds
        self._breaker = breaker or cache_breaker()

    @classmethod
    def disabled(cls) -> Cache:
        """A cache that stores nothing and always misses.

        For a deployment configured without Redis, and for the tests that have
        to show the service answers the same way without one.
        """
        return cls(None, ttl_seconds=0)

    @property
    def is_enabled(self) -> bool:
        """Whether there is a client behind this cache at all."""
        return self._client is not None

    async def get(self, key: str) -> bytes | None:
        """Read one value, or nothing if it is absent or unreachable."""
        if self._client is None or self._bypassed("get"):
            return None

        try:
            value = await self._client.get(key)
        except _FAILURES as error:
            self._report("get", CacheOutcome.ERROR, error)
            return None
        self._breaker.record_success()

        outcome = CacheOutcome.MISS if value is None else CacheOutcome.HIT
        CACHE_OPERATIONS.labels(operation="get", outcome=outcome).inc()
        return value if isinstance(value, bytes) else None

    async def set(self, key: str, value: bytes, *, ttl_seconds: int | None = None) -> None:
        """Store one value under the configured time to live.

        A TTL is always set, never omitted: a key without one survives every
        deployment and every schema change, and the first thing anyone does
        with a cache that has no expiry is flush it by hand during an incident.
        """
        if self._client is None or self._bypassed("set"):
            return

        try:
            await self._client.set(key, value, ex=ttl_seconds or self._ttl_seconds)
        except _FAILURES as error:
            self._report("set", CacheOutcome.ERROR, error)
            return
        self._breaker.record_success()

        CACHE_OPERATIONS.labels(operation="set", outcome=CacheOutcome.STORED).inc()

    async def increment(self, key: str) -> int | None:
        """Add one to a counter, creating it at 1 if it is absent.

        The operation a generation-based invalidation is built on: one atomic
        command, no scanning, and nothing to enumerate.
        """
        if self._client is None or self._bypassed("increment"):
            return None

        try:
            value = await self._client.incr(key)
        except _FAILURES as error:
            self._report("increment", CacheOutcome.ERROR, error)
            return None
        self._breaker.record_success()

        CACHE_OPERATIONS.labels(operation="increment", outcome=CacheOutcome.STORED).inc()
        return int(value)

    async def aclose(self) -> None:
        """Return the connections to the pool and close it.

        Called from the lifespan shutdown. Failures are swallowed here too: a
        cache that cannot be closed cleanly must not stop a pod from exiting.
        """
        if self._client is None:
            return
        try:
            await self._client.aclose()
        except _FAILURES as error:
            self._report("close", CacheOutcome.ERROR, error)

    def _bypassed(self, operation: str) -> bool:
        """Whether the breaker is open and this operation is not to be tried."""
        if self._breaker.state is not CircuitState.OPEN:
            return False
        CACHE_OPERATIONS.labels(operation=operation, outcome=CacheOutcome.BYPASSED).inc()
        return True

    def _report(self, operation: str, outcome: str, error: BaseException) -> None:
        """Record a failure without letting it reach the caller.

        Logged as ``WARNING``, since the request is still served from the
        database. The key is not included in the record. The breaker counts
        it; once open, it is what logs, and the operations stop failing.
        """
        self._breaker.record_failure()
        CACHE_OPERATIONS.labels(operation=operation, outcome=outcome).inc()
        _logger.warning(
            "cache is unavailable",
            cache_operation=operation,
            error=type(error).__name__,
        )
