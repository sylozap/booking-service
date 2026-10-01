"""How often a caller may knock: a sliding window counter in Redis.

Per window and per caller, Redis holds one counter. The estimate of the last
``window`` seconds weighs the previous window's counter by the share of it
that still falls inside the sliding window, and adds the current one::

    estimate = previous * (window - elapsed) / window + current

Two integers per caller rather than a log of every request, and no reset at the
boundary of a minute: thirty requests at 12:00:59 still count at 12:01:00. The
price is an estimate that assumes the previous window's requests were spread
evenly, which is the usual trade (it is what docs/04 names, "sliding window
counter"). Reading both counters and adding to the current one is one Lua
script, so two gateway replicas cannot both let the last request through.

The clock is the gateway's, passed to the script, rather than Redis's ``TIME``:
replicas are a few milliseconds apart at most, and a test can move it.

**Redis being down lets the traffic through.** A limiter that cannot count
does not refuse: on this platform availability matters more than protection
from a burst -- a stand without Redis is slower, not closed. The failure is a
``WARNING`` in the log, at most once per ``_FAILURE_LOG_INTERVAL_SECONDS``, so
an outage of Redis does not turn into a flood of identical records.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from redis.asyncio import Redis
from redis.exceptions import RedisError
from starlette.requests import Request

from barber_common.logging import get_logger

__all__ = [
    "Decision",
    "Limit",
    "LimitScope",
    "RateLimitPolicy",
    "SlidingWindowLimiter",
    "client_address",
    "retry_after_seconds",
    "sliding_estimate",
]

_logger = get_logger(__name__)

_FAILURES = (RedisError, OSError, TimeoutError)
_FAILURE_LOG_INTERVAL_SECONDS = 30.0

# KEYS: the counter of the current window, the counter of the previous one.
# ARGV: the limit, the weight of the previous window, the TTL in milliseconds.
# Returns whether the request is admitted and both counters as they were seen.
_HIT_SCRIPT = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local previous = tonumber(redis.call('GET', KEYS[2]) or '0')
local limit = tonumber(ARGV[1])
local weight = tonumber(ARGV[2])
if previous * weight + current + 1 > limit then
  return {0, current, previous}
end
redis.call('INCR', KEYS[1])
redis.call('PEXPIRE', KEYS[1], ARGV[3])
return {1, current, previous}
"""


class LimitScope(StrEnum):
    """What a limit counts. Part of the Redis key and of the log record."""

    ANONYMOUS = "anonymous"
    USER = "user"
    BOOKING_CREATE = "booking_create"
    REGISTRATION = "registration"


@dataclass(frozen=True, slots=True)
class Limit:
    """At most ``requests`` per sliding ``window_seconds``."""

    scope: LimitScope
    requests: int
    window_seconds: int

    def __post_init__(self) -> None:
        if self.requests < 1 or self.window_seconds < 1:
            raise ValueError("a limit admits at least one request per at least one second")


@dataclass(frozen=True, slots=True)
class Decision:
    """Whether a request is admitted, and if not, how long to wait."""

    allowed: bool
    retry_after_seconds: int = 0


def sliding_estimate(*, previous: int, current: int, elapsed_ms: int, window_ms: int) -> float:
    """Requests in the last window, as the counter estimates them."""
    return previous * (window_ms - elapsed_ms) / window_ms + current


def retry_after_seconds(
    *, limit: int, previous: int, current: int, elapsed_ms: int, window_ms: int
) -> int:
    """How long until one more request fits under the limit, in whole seconds.

    Assumes nobody else asks in the meantime, which is what ``Retry-After``
    can promise at best. Two cases. If the current window still has room, the
    wait is until enough of the previous window has slid out. If it is full,
    the wait runs into the next window, where the current counter becomes the
    one sliding out. Never less than a second: ``Retry-After: 0`` invites an
    immediate retry that is refused again.
    """
    room = limit - 1
    remaining_ms = window_ms - elapsed_ms

    if current <= room:
        # previous * (remaining - wait) / window + current <= room
        wait_ms = remaining_ms - (room - current) * window_ms / previous if previous else 0.0
    else:
        # In the next window: current * (window - into_next) / window <= room
        wait_ms = remaining_ms + window_ms - room * window_ms / current

    return max(1, math.ceil(max(wait_ms, 0.0) / 1000))


class SlidingWindowLimiter:
    """Counts requests in Redis and admits those under the limit."""

    def __init__(
        self,
        redis: Redis | None,
        *,
        clock: Callable[[], float] = time.time,
        key_prefix: str = "ratelimit",
    ) -> None:
        self._redis = redis
        self._clock = clock
        self._key_prefix = key_prefix
        self._last_failure_logged_at: float | None = None

    @classmethod
    def disabled(cls) -> SlidingWindowLimiter:
        """A limiter that admits everything, for a gateway configured without one."""
        return cls(None)

    async def aclose(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()

    async def hit(self, limit: Limit, subject: str) -> Decision:
        """Count one request of ``subject`` against ``limit``, if it fits."""
        if self._redis is None:
            return Decision(allowed=True)

        window_ms = limit.window_seconds * 1000
        now_ms = int(self._clock() * 1000)
        index, elapsed_ms = divmod(now_ms, window_ms)
        weight = (window_ms - elapsed_ms) / window_ms
        base = f"{self._key_prefix}:{limit.scope}:{subject}"

        try:
            admitted, current, previous = await self._redis.eval(
                _HIT_SCRIPT,
                2,
                f"{base}:{index}",
                f"{base}:{index - 1}",
                limit.requests,
                repr(weight),
                # Two windows: the counter is still read as "previous" for the
                # whole of the next one.
                2 * window_ms,
            )
        except _FAILURES as error:
            self._report_failure(error)
            return Decision(allowed=True)

        if admitted:
            return Decision(allowed=True)
        return Decision(
            allowed=False,
            retry_after_seconds=retry_after_seconds(
                limit=limit.requests,
                previous=int(previous),
                current=int(current),
                elapsed_ms=elapsed_ms,
                window_ms=window_ms,
            ),
        )

    def _report_failure(self, error: BaseException) -> None:
        now = time.monotonic()
        if (
            self._last_failure_logged_at is not None
            and now - self._last_failure_logged_at < _FAILURE_LOG_INTERVAL_SECONDS
        ):
            return
        self._last_failure_logged_at = now
        _logger.warning(
            "rate limiter cannot reach redis, letting requests through",
            error=type(error).__name__,
        )


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    """Which limits a request counts against, and as whom.

    Every request counts against the general limit of its caller: the user
    when the token checked out, the address otherwise. Two kinds of request
    count against a strict limit on top of it, because each one costs more
    than a read -- a booking holds a slot, a registration sends a letter.
    """

    anonymous: Limit
    user: Limit
    booking_create: Limit
    registration: Limit

    def limits_for(
        self, *, method: str, path: str, user_id: str | None, client_ip: str
    ) -> list[tuple[Limit, str]]:
        """The limits in the order they are checked, each with its subject."""
        caller = user_id or client_ip
        limits = [(self.user, user_id) if user_id else (self.anonymous, client_ip)]
        if method == "POST" and path == "/api/v1/bookings":
            limits.append((self.booking_create, caller))
        if method == "POST" and path == "/api/v1/auth/register":
            # By address: whoever registers has no account yet. The only
            # endpoint that sends a confirmation letter, so this is the limit
            # docs/04 calls "sending a confirmation letter".
            limits.append((self.registration, client_ip))
        return limits


def client_address(request: Request, *, trusted_proxy_hops: int) -> str:
    """The address of the client, as far as it can be trusted.

    ``X-Forwarded-For`` is written by whoever sent the request, so only the
    entries appended by proxies the platform runs are believed: the last
    ``trusted_proxy_hops`` of them, counting the peer of the connection. With
    no trusted proxy -- the local stack, where clients connect directly -- the
    header is ignored entirely; otherwise a client would choose its own address
    and with it a fresh anonymous quota per request.
    """
    peer = request.client.host if request.client is not None else "unknown"
    if trusted_proxy_hops <= 0:
        return peer

    forwarded = [
        address.strip()
        for address in request.headers.get("x-forwarded-for", "").split(",")
        if address.strip()
    ]
    chain = [*forwarded, peer]
    return chain[max(len(chain) - 1 - trusted_proxy_hops, 0)]
