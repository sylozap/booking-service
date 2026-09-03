"""Asynchronous SQLAlchemy engine.

One engine per process, created at startup and disposed on ``SIGTERM``. The
pool is bounded on purpose: an unbounded pool hides a leak until the database
refuses new connections.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from barber_common.config import BaseAppSettings

__all__ = ["create_engine", "create_engine_from_settings"]


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
