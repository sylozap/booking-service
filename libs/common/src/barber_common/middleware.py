"""ASGI middleware of the chassis.

Written against the raw ASGI interface rather than ``BaseHTTPMiddleware``: the
latter runs the handler in a task of its own, and a context variable set there
does not reliably reach the endpoint. The correlation id has to reach it.

Order matters. Correlation id is installed first, before anything can log or
fail, otherwise the earliest records of a request -- the interesting ones when
something goes wrong -- carry no identifier at all.
"""

from __future__ import annotations

import asyncio
import time
from uuid import uuid4

from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from barber_common.context import bind_context
from barber_common.logging import get_logger

__all__ = [
    "CORRELATION_ID_HEADER",
    "AccessLogMiddleware",
    "CorrelationIdMiddleware",
    "InFlightRequestsMiddleware",
    "RequestTracker",
]

CORRELATION_ID_HEADER = "x-correlation-id"

_logger = get_logger(__name__)


def _route_template(scope: Scope) -> str:
    """Return the route template of a request, never the concrete URL.

    A metric label or a log field holding ``/bookings/0192f3c1-...`` is an
    identifier in disguise and explodes the cardinality of both.
    """
    route = scope.get("route")
    if isinstance(route, Route):
        return route.path
    return str(scope.get("path", ""))


class CorrelationIdMiddleware:
    """Accept the correlation id of the caller or mint one, and give it back.

    The gateway sets the header for a request coming from outside; a service
    called by another service receives it in the same header. Either way one
    identifier spans the synchronous calls, the broker and the notification.
    """

    def __init__(self, app: ASGIApp, *, header_name: str = CORRELATION_ID_HEADER) -> None:
        self.app = app
        self.header_name = header_name.lower()
        self._encoded_header = self.header_name.encode("latin-1")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        correlation_id = self._read_header(scope) or uuid4().hex

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((self._encoded_header, correlation_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        with bind_context(correlation_id=correlation_id):
            await self.app(scope, receive, send_with_header)

    def _read_header(self, scope: Scope) -> str | None:
        for name, value in scope.get("headers", []):
            if name.decode("latin-1").lower() == self.header_name:
                candidate = value.decode("latin-1").strip()
                return candidate or None
        return None


class AccessLogMiddleware:
    """One structured record per request, with the route template and the status."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started_at = time.perf_counter()
        status_code = 500

        async def send_with_status(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_with_status)
        finally:
            duration = time.perf_counter() - started_at
            # An expected client error is not an incident; only 5xx is logged
            # as an error, or the error rate alert fires during normal work.
            log = _logger.error if status_code >= 500 else _logger.info
            log(
                "request handled",
                http_method=str(scope.get("method", "")),
                http_path=_route_template(scope),
                http_status=status_code,
                duration_seconds=round(duration, 6),
            )


class RequestTracker:
    """Counts the requests being handled right now.

    Kubernetes sends ``SIGTERM`` and removes the pod from the endpoints of the
    Service at the same moment, not in that order. Shutting down immediately
    therefore cuts requests that are already in flight; the lifespan waits for
    this counter to reach zero instead.
    """

    def __init__(self) -> None:
        self._in_flight = 0
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def in_flight(self) -> int:
        return self._in_flight

    def enter(self) -> None:
        self._in_flight += 1
        self._idle.clear()

    def leave(self) -> None:
        self._in_flight -= 1
        if self._in_flight <= 0:
            self._idle.set()

    async def wait_until_idle(self, *, timeout_seconds: float) -> bool:
        """Wait for the last request to finish. Returns False if time ran out."""
        try:
            async with asyncio.timeout(timeout_seconds):
                await self._idle.wait()
        except TimeoutError:
            return False
        return True


class InFlightRequestsMiddleware:
    """Feeds the :class:`RequestTracker` around every request."""

    def __init__(self, app: ASGIApp, tracker: RequestTracker) -> None:
        self.app = app
        self.tracker = tracker

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        self.tracker.enter()
        try:
            await self.app(scope, receive, send)
        finally:
            self.tracker.leave()
