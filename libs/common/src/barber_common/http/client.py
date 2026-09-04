"""HTTP client for calls between services.

Every rule here exists because of a specific failure:

* separate connect and read timeouts, because a call without a timeout holds a
  connection until the pool of the caller is empty;
* retries for idempotent methods only, because retrying a POST creates a second
  booking, and no header saves a request that already succeeded;
* a circuit breaker, because a service that is down should cost one failure,
  not one timeout per request;
* translation of a foreign ``problem+json`` into an exception, so the caller
  reads a domain code instead of guessing from a status.

A service calls another service through this class and only from ``clients/``.
A bare ``httpx`` call somewhere else has none of the above and is a defect.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import TracebackType
from typing import Self

import httpx

from barber_common.context import get_correlation_id
from barber_common.errors import DomainError
from barber_common.http.circuit_breaker import CircuitBreaker, UpstreamUnavailable
from barber_common.logging import get_logger
from barber_common.middleware import CORRELATION_ID_HEADER

__all__ = [
    "IDEMPOTENT_METHODS",
    "RetryPolicy",
    "ServiceClient",
    "Timeouts",
    "UpstreamError",
]

# A retry of anything else creates a duplicate. POST /bookings is protected by
# an Idempotency-Key, but that is the contract of one endpoint, not a rule of
# the transport, so it does not make POST retriable here.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})

_logger = get_logger(__name__)


class UpstreamError(DomainError):
    """Another service answered with an error.

    Carries the status and the domain code of the other side, so the calling
    scenario can tell "the master does not exist" from "catalog is broken".
    """

    code = "upstream_error"
    http_status = 502
    title = "Upstream service returned an error"

    def __init__(
        self,
        *,
        upstream: str,
        status_code: int,
        remote_code: str | None,
        detail: str,
    ) -> None:
        self.upstream = upstream
        self.status_code = status_code
        self.remote_code = remote_code
        super().__init__(
            detail,
            extra={
                "upstream": upstream,
                "upstream_status": status_code,
                "upstream_code": remote_code,
            },
        )


@dataclass(frozen=True, slots=True)
class Timeouts:
    """Separate deadlines, because they fail for different reasons.

    A connect timeout expiring means the service is not there; a read timeout
    expiring means it is there and slow.
    """

    connect_seconds: float = 1.0
    read_seconds: float = 3.0
    write_seconds: float = 3.0
    pool_seconds: float = 1.0

    def to_httpx(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect_seconds,
            read=self.read_seconds,
            write=self.write_seconds,
            pool=self.pool_seconds,
        )


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Exponential backoff with jitter.

    Jitter matters more than the backoff: without it every caller of a service
    that just came back retries at the same millisecond and knocks it over
    again.
    """

    attempts: int = 3
    initial_delay_seconds: float = 0.1
    multiplier: float = 2.0
    max_delay_seconds: float = 2.0
    jitter_ratio: float = 0.2
    retry_statuses: frozenset[int] = field(default_factory=lambda: frozenset({502, 503, 504}))

    def delay_for(self, attempt: int) -> float:
        """Delay before the given attempt, counted from 1."""
        delay = min(
            self.initial_delay_seconds * self.multiplier ** (attempt - 1),
            self.max_delay_seconds,
        )
        # Not a security decision, so the fast generator is the right one.
        jitter = delay * self.jitter_ratio * random.random()  # noqa: S311
        return delay + jitter


class ServiceClient:
    """Client of one other service."""

    def __init__(
        self,
        *,
        base_url: str,
        upstream: str,
        timeouts: Timeouts | None = None,
        retry: RetryPolicy | None = None,
        breaker: CircuitBreaker | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.upstream = upstream
        self.retry = retry or RetryPolicy()
        self.breaker = breaker or CircuitBreaker(name=upstream)
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=(timeouts or Timeouts()).to_httpx(),
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        service_token: str | None = None,
    ) -> httpx.Response:
        return await self.request(
            "GET", url, params=params, headers=headers, service_token=service_token
        )

    async def post(
        self,
        url: str,
        *,
        json: object = None,
        headers: Mapping[str, str] | None = None,
        service_token: str | None = None,
    ) -> httpx.Response:
        return await self.request(
            "POST", url, json=json, headers=headers, service_token=service_token
        )

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        json: object = None,
        headers: Mapping[str, str] | None = None,
        service_token: str | None = None,
    ) -> httpx.Response:
        """Send a request, retrying it only when that is safe.

        Raises :class:`UpstreamUnavailable` when the breaker is open or the
        service never answered, and :class:`UpstreamError` when it answered
        with a status the caller did not ask for.
        """
        self.breaker.check()

        attempts = self.retry.attempts if method.upper() in IDEMPOTENT_METHODS else 1
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers=self._headers(headers, service_token),
                )
            except httpx.TimeoutException as error:
                last_error = error
                self.breaker.record_failure()
            except httpx.TransportError as error:
                last_error = error
                self.breaker.record_failure()
            else:
                if response.status_code not in self.retry.retry_statuses:
                    self.breaker.record_success()
                    return self._checked(response)

                last_error = None
                self.breaker.record_failure()
                if attempt == attempts:
                    return self._checked(response)

            if attempt == attempts:
                break

            delay = self.retry.delay_for(attempt)
            _logger.warning(
                "retrying a call to another service",
                upstream=self.upstream,
                http_method=method,
                attempt=attempt,
                delay_seconds=round(delay, 3),
            )
            await asyncio.sleep(delay)

        raise UpstreamUnavailable(
            f"{self.upstream} did not answer after {attempts} attempt(s)",
            extra={"upstream": self.upstream},
        ) from last_error

    def _headers(
        self, headers: Mapping[str, str] | None, service_token: str | None
    ) -> dict[str, str]:
        """Carry the correlation id and the service token of the caller."""
        result = dict(headers or {})
        correlation_id = get_correlation_id()
        if correlation_id is not None:
            result[CORRELATION_ID_HEADER] = correlation_id
        if service_token is not None:
            result["Authorization"] = f"Bearer {service_token}"
        return result

    def _checked(self, response: httpx.Response) -> httpx.Response:
        """Turn an error answer into an exception carrying the foreign code."""
        if response.is_success:
            return response

        remote_code, detail = _read_problem(response)
        raise UpstreamError(
            upstream=self.upstream,
            status_code=response.status_code,
            remote_code=remote_code,
            detail=detail,
        )


def _read_problem(response: httpx.Response) -> tuple[str | None, str]:
    """Read the domain code out of a foreign ``problem+json``.

    A service that answers with something else -- a proxy page, an empty body --
    is not a reason to fail here, so the status becomes the description.
    """
    fallback = f"{response.status_code} from the upstream service"
    if "problem+json" not in response.headers.get("content-type", ""):
        return None, fallback

    try:
        document = response.json()
    except ValueError:
        return None, fallback

    if not isinstance(document, dict):
        return None, fallback

    code = document.get("code")
    detail = document.get("detail")
    return (
        code if isinstance(code, str) else None,
        detail if isinstance(detail, str) else fallback,
    )
