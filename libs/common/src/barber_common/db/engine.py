"""Asynchronous SQLAlchemy engine.

One engine per process, created at startup and disposed on ``SIGTERM``. The
pool is bounded on purpose: an unbounded pool hides a leak until the database
refuses new connections.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import QueuePool

from barber_common.config import BaseAppSettings
from barber_common.metrics import gauge

__all__ = ["DB_POOL_USAGE", "create_engine", "create_engine_from_settings", "observe_pool"]

# Near 1 for minutes: requests queue for a connection and soon fail on the pool
# timeout. Either the pool is too small for the load, or something holds a
# connection too long -- a network call inside a transaction is the usual one.
#
# Labelled by database so that a service without one -- the gateway -- reports
# nothing rather than a flat zero.
DB_POOL_USAGE = gauge(
    "db_pool_usage_ratio",
    "Connections checked out of the pool, as a share of pool size plus overflow",
    labelnames=("database",),
)


def create_engine(
    dsn: str,
    *,
    pool_size: int = 10,
    max_overflow: int = 5,
    pool_timeout_seconds: float = 5.0,
    echo: bool = False,
) -> AsyncEngine:
    """Build the engine for a service database.

    ``pool_pre_ping`` costs one round trip per checkout and saves the first
    request after a database restart or a connection dropped by a proxy.
    """
    return create_async_engine(
        dsn,
        echo=echo,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout_seconds,
        pool_pre_ping=True,
        future=True,
    )


def create_engine_from_settings(settings: BaseAppSettings) -> AsyncEngine:
    """Build the engine from the settings of a service."""
    return create_engine(
        str(settings.database_dsn.get_secret_value()),
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout_seconds=settings.database_pool_timeout_seconds,
        echo=settings.database_echo,
    )


def observe_pool(engine: AsyncEngine, *, capacity: int) -> None:
    """Report how full the pool of the engine is, read at every scrape.

    ``capacity`` is pool size plus overflow from the settings: SQLAlchemy
    reports the connections checked out, but not the overflow ceiling.
    """
    pool = engine.sync_engine.pool
    if not isinstance(pool, QueuePool) or capacity <= 0:
        return
    database = engine.url.database or "default"
    DB_POOL_USAGE.labels(database=database).set_function(lambda: pool.checkedout() / capacity)
