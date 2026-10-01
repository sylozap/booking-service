"""Outgoing calls to other services."""

from barber_common.http.circuit_breaker import CircuitBreaker, CircuitState, UpstreamUnavailable
from barber_common.http.client import (
    IDEMPOTENT_METHODS,
    RetryPolicy,
    ServiceClient,
    Timeouts,
    UpstreamError,
    UpstreamTimeout,
)

__all__ = [
    "IDEMPOTENT_METHODS",
    "CircuitBreaker",
    "CircuitState",
    "RetryPolicy",
    "ServiceClient",
    "Timeouts",
    "UpstreamError",
    "UpstreamTimeout",
    "UpstreamUnavailable",
]
