"""Settings of the api-gateway service."""

from __future__ import annotations

from barber_common.config import BaseAppSettings, SecretPostgresDsn

__all__ = ["GatewaySettings"]


class GatewaySettings(BaseAppSettings):
    """Everything the chassis needs, minus the database it does not have."""

    service_name: str = "api-gateway"

    # The gateway owns no data (docs/03-services.md): it proxies, and the only
    # state it touches is the Redis of the rate limiter. Keeping the DSN
    # mandatory would force an operator to invent a database that never exists.
    # Narrowing a required field of the chassis to optional is the point here,
    # which is what the ignore is for: this is the one service without a database.
    database_dsn: SecretPostgresDsn | None = None  # type: ignore[assignment]
