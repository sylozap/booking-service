"""Settings of the api-gateway service."""

from __future__ import annotations

from barber_common.config import BaseAppSettings, SecretPostgresDsn

__all__ = ["GatewaySettings"]


class GatewaySettings(BaseAppSettings):
    """Everything the chassis needs, minus the database it does not have."""

    service_name: str = "api-gateway"

    # The gateway owns no database, so the DSN required by the chassis is
    # narrowed to optional here; hence the ignore.
    database_dsn: SecretPostgresDsn | None = None  # type: ignore[assignment]
