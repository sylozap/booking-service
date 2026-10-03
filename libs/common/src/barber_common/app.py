"""Application factory: everything a service shares, assembled once.

A service is left with its settings and its routers::

    settings = BookingSettings.load()
    app = create_app(settings, routers=[bookings_router, availability_router])

What it gets in return: correlation id, structured access log, RFC 9457 error
handlers, probes, metrics, tracing, and an orderly shutdown.

The shutdown, as it happens in a cluster. Kubernetes deletes the pod and, at
the same moment, starts removing it from the endpoints of the Service; the
``preStop`` pause of the chart keeps the pod serving while that removal
reaches every node. Then SIGTERM: uvicorn stops accepting connections and
waits for the requests in flight (``UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN``), and
only then runs the lifespan shutdown below -- readiness reports draining,
the service stops its consumers, relay and workers, the pool is closed. The
grace period of the pod is the sum of these phases, and the chart computes it
(``deploy/helm/service/values.yaml``, ``shutdown``).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager

from fastapi import APIRouter, FastAPI

from barber_common.config import BaseAppSettings
from barber_common.db.engine import observe_pool
from barber_common.db.session import Database
from barber_common.errors import document_problem_responses, install_error_handlers
from barber_common.health import HealthRegistry, create_health_router, database_check
from barber_common.logging import configure_logging, get_logger
from barber_common.metrics import instrument_app
from barber_common.middleware import (
    AccessLogMiddleware,
    CorrelationIdMiddleware,
    InFlightRequestsMiddleware,
    RequestTracker,
)
from barber_common.tracing import configure_tracing, instrument_engine, instrument_fastapi

__all__ = ["ServiceLifespan", "create_app", "use_database"]

# A service adds its own startup and shutdown around the one of the chassis.
ServiceLifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]

_logger = get_logger(__name__)

DEFAULT_DRAIN_TIMEOUT_SECONDS = 20.0


def use_database(app: FastAPI, database: Database) -> None:
    """Attach a database to the application.

    Puts it where the ``get_session`` dependency looks for it, adds it to the
    readiness probe, gives its statements their own spans and reports how full
    its pool is.
    """
    app.state.database = database
    health: HealthRegistry = app.state.health
    health.register("database", database_check(database))
    instrument_engine(database.engine)
    settings: BaseAppSettings = app.state.settings
    observe_pool(
        database.engine,
        capacity=settings.database_pool_size + settings.database_max_overflow,
    )


def create_app(
    settings: BaseAppSettings,
    *,
    routers: Sequence[APIRouter] = (),
    lifespan: ServiceLifespan | None = None,
    title: str | None = None,
    version: str = "1",
    drain_timeout_seconds: float = DEFAULT_DRAIN_TIMEOUT_SECONDS,
    openapi_url: str | None = "/openapi.json",
) -> FastAPI:
    """Build the FastAPI application of a service.

    ``openapi_url`` is ``None`` for the gateway, which serves a document of the
    whole platform in its place rather than one of its own few routes.
    """
    configure_logging(
        service_name=settings.service_name,
        environment=settings.environment,
        log_level=settings.log_level,
    )
    configure_tracing(settings)

    health = HealthRegistry()
    tracker = RequestTracker()

    @asynccontextmanager
    async def chassis_lifespan(app: FastAPI) -> AsyncIterator[None]:
        _logger.info(
            "service starting",
            environment=settings.environment.value,
            version=version,
        )
        phases: dict[str, float] = {}
        async with AsyncExitStack() as stack:
            if lifespan is not None:
                await stack.enter_async_context(lifespan(app))
            try:
                yield
            finally:
                _logger.info("shutdown started", in_flight=tracker.in_flight)
                started = time.monotonic()
                await _drain(health, tracker, drain_timeout_seconds)
                phases["requests_seconds"] = time.monotonic() - started
            # Leaving the block stops what the service started, in reverse:
            # the consumers finish the message in hand and commit.
            background_started = time.monotonic()
        phases["background_seconds"] = time.monotonic() - background_started
        await _close_database(app)
        # How long each phase took, against the budget the chart gives the pod:
        # a shutdown that keeps growing is cut by SIGKILL one day.
        _logger.info(
            "service stopped",
            **{name: round(seconds, 3) for name, seconds in phases.items()},
            total_seconds=round(time.monotonic() - started, 3),
        )

    app = FastAPI(
        title=title or settings.service_name,
        version=version,
        lifespan=chassis_lifespan,
        openapi_url=openapi_url,
    )
    app.state.settings = settings
    app.state.health = health
    app.state.requests = tracker

    install_error_handlers(app)
    document_problem_responses(app)
    app.include_router(
        create_health_router(health, timeout_seconds=settings.health_check_timeout_seconds)
    )
    for router in routers:
        app.include_router(router)

    # Middleware is entered in reverse order of registration, so this reads
    # inside out: metrics sit closest to the endpoint, the correlation id
    # wraps everything. The access log is inside the tracing middleware on
    # purpose -- outside it the span has already ended and the record would
    # carry no trace_id, which is the field that makes it worth reading.
    instrument_app(app)
    app.add_middleware(AccessLogMiddleware)
    instrument_fastapi(app)
    app.add_middleware(InFlightRequestsMiddleware, tracker=tracker)
    app.add_middleware(CorrelationIdMiddleware)

    return app


async def _drain(health: HealthRegistry, tracker: RequestTracker, timeout_seconds: float) -> None:
    """Fail the readiness probe, then wait for the requests in flight.

    Under uvicorn the requests have usually finished by now: it waits for them
    before the lifespan shutdown starts. The wait here covers a server that
    does not, and the readiness answer covers a shutdown nobody deleted the
    pod for -- the probe is then the only thing that takes it out of the
    endpoints.
    """
    health.start_draining()
    in_flight = tracker.in_flight
    if in_flight:
        _logger.info("draining requests", in_flight=in_flight)

    if not await tracker.wait_until_idle(timeout_seconds=timeout_seconds):
        _logger.warning(
            "shutdown deadline reached with requests still running",
            in_flight=tracker.in_flight,
            timeout_seconds=timeout_seconds,
        )


async def _close_database(app: FastAPI) -> None:
    """Return every pooled connection before the process exits."""
    database = getattr(app.state, "database", None)
    if isinstance(database, Database):
        await database.dispose()
