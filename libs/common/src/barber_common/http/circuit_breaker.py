"""Circuit breaker for calls to another service.

Without it, a service that is down costs every caller a full timeout per
request. The threads pile up, the pool of the caller runs out, and an outage of
one service becomes an outage of three. The breaker turns that into an
immediate failure, which the caller can answer with a fallback or a 503.

Three states, the usual ones. Closed lets calls through and counts failures.
After ``failure_threshold`` in a row it opens and rejects everything for
``reset_timeout_seconds``. Then it half-opens and lets a single call through:
success closes it, failure opens it again.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import StrEnum

from barber_common.errors import DomainError
from barber_common.logging import get_logger

__all__ = ["CircuitBreaker", "CircuitState", "UpstreamUnavailable"]

_logger = get_logger(__name__)


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class UpstreamUnavailable(DomainError):
    """The service being called is not answering, or the breaker is open."""

    code = "upstream_unavailable"
    http_status = 503
    title = "Upstream service is unavailable"


class CircuitBreaker:
    """State of one route to one service.

    ``clock`` is injectable so tests can move time instead of sleeping through
    the reset timeout.
    """

    def __init__(
        self,
        *,
        name: str,
        failure_threshold: int = 5,
        reset_timeout_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold has to be at least 1")

        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout_seconds = reset_timeout_seconds
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0

    @property
    def state(self) -> CircuitState:
        """Current state, after taking the reset timeout into account."""
        if (
            self._state is CircuitState.OPEN
            and self._clock() - self._opened_at >= self.reset_timeout_seconds
        ):
            self._state = CircuitState.HALF_OPEN
            _logger.info("circuit breaker half-opened", upstream=self.name)
        return self._state

    def check(self) -> None:
        """Raise if the call must not be made at all."""
        if self.state is CircuitState.OPEN:
            raise UpstreamUnavailable(
                f"circuit breaker for {self.name} is open",
                extra={"upstream": self.name},
            )

    def record_success(self) -> None:
        if self._state is not CircuitState.CLOSED:
            _logger.info("circuit breaker closed", upstream=self.name)
        self._state = CircuitState.CLOSED
        self._failures = 0

    def record_failure(self) -> None:
        # A failure while half-open means the service is still down: open again
        # for the full timeout rather than letting the next caller find out.
        if self._state is CircuitState.HALF_OPEN:
            self._trip()
            return

        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._trip()

    def _trip(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = self._clock()
        self._failures = 0
        _logger.warning(
            "circuit breaker opened",
            upstream=self.name,
            reset_timeout_seconds=self.reset_timeout_seconds,
        )
