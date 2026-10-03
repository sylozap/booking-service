"""Liveness and readiness probes.

The two are deliberately different. ``/health/live`` answers whether the
process itself is alive and touches nothing external; ``/health/ready``
answers whether the instance can serve traffic and asks the dependencies the
service registered.

Mixing them is the classic outage amplifier: a database hiccup turns a
readiness failure into a restart loop of otherwise healthy pods.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import APIRouter, Response
from starlette import status

from barber_common.db.session import Database
from barber_common.logging import get_logger

__all__ = [
    "CheckOutcome",
    "HealthRegistry",
    "HealthCheck",
    "create_health_router",
    "database_check",
]

# A check reports success by returning and failure by raising.
HealthCheck = Callable[[], Awaitable[None]]

_logger = get_logger(__name__)


async def _run(check: HealthCheck) -> None:
    """Adapt a check to a coroutine, the only thing ``create_task`` accepts."""
    await check()


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    """Result of one readiness check."""

    name: str
    is_healthy: bool
    detail: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": "ok" if self.is_healthy else "fail",
            "detail": self.detail,
        }


class HealthRegistry:
    """Checks a service wants the readiness probe to run.

    The registry is filled at startup — database, Redis, Kafka — and belongs to
    the application instance, not to the module: two applications in one test
    process must not share it.
    """

    def __init__(self) -> None:
        self._checks: dict[str, HealthCheck] = {}
        self._is_draining = False

    @property
    def is_draining(self) -> bool:
        return self._is_draining

    def start_draining(self) -> None:
        """Report not ready from now on, while still serving what is in flight.

        Called when the shutdown begins. A pod that is being deleted leaves the
        endpoints of the Service without waiting for this -- Kubernetes removes
        it as part of the deletion, and the ``preStop`` pause of the chart
        covers the time that takes. A failed readiness probe is what removes a
        pod that is shutting down for any other reason.
        """
        self._is_draining = True

    def register(self, name: str, check: HealthCheck) -> None:
        """Add a check. A repeated name is a mistake, not an override."""
        if name in self._checks:
            raise ValueError(f"health check {name!r} is already registered")
        self._checks[name] = check

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._checks)

    async def run(self, *, timeout_seconds: float) -> list[CheckOutcome]:
        """Run every check in parallel under one shared deadline.

        The deadline is shared on purpose: three dependencies with a two second
        timeout each must not make the probe answer in six.
        """
        if not self._checks:
            return []

        names = list(self._checks)
        tasks: list[asyncio.Task[None]] = [
            asyncio.create_task(_run(self._checks[name]), name=name) for name in names
        ]
        done, pending = await asyncio.wait(tasks, timeout=timeout_seconds)

        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        outcomes: list[CheckOutcome] = []
        for name, task in zip(names, tasks, strict=True):
            if task not in done:
                outcomes.append(CheckOutcome(name, is_healthy=False, detail="timed out"))
                continue
            error = task.exception()
            if error is None:
                outcomes.append(CheckOutcome(name, is_healthy=True))
            else:
                outcomes.append(CheckOutcome(name, is_healthy=False, detail=type(error).__name__))
        return outcomes


def database_check(database: Database) -> HealthCheck:
    """Build the readiness check of the service database."""

    async def check() -> None:
        await database.check_connection()

    return check


def create_health_router(registry: HealthRegistry, *, timeout_seconds: float = 2.0) -> APIRouter:
    """Build the router with both probes.

    Excluded from the OpenAPI schema: probes are an operational contract with
    Kubernetes, not part of the public API.
    """
    router = APIRouter(tags=["health"], include_in_schema=False)

    @router.get("/health/live")
    async def live() -> dict[str, str]:
        """Answer as long as the event loop runs. No external calls, ever."""
        return {"status": "ok"}

    @router.get("/health/ready")
    async def ready(response: Response) -> dict[str, object]:
        """Answer whether this instance can serve traffic right now."""
        if registry.is_draining:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "draining", "checks": []}

        outcomes = await registry.run(timeout_seconds=timeout_seconds)
        failed = [outcome for outcome in outcomes if not outcome.is_healthy]

        if failed:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            _logger.warning(
                "readiness check failed",
                failed_checks=[outcome.name for outcome in failed],
            )

        return {
            "status": "fail" if failed else "ok",
            "checks": [outcome.as_dict() for outcome in outcomes],
        }

    return router
