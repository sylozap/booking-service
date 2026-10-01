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

    # --- the services behind the gateway -----------------------------------
    # Base URLs, Kubernetes Service DNS in a cluster. No defaults: a gateway
    # pointed at nothing would start and answer 503 to everything.
    auth_url: str
    catalog_url: str
    booking_url: str
    notification_url: str

    # --- proxying ----------------------------------------------------------
    # The connect deadline is short: a service that does not accept a
    # connection within a second is not there. The read deadline is per chunk
    # of the answer, not for the whole of it, and covers the slowest endpoint
    # of the platform -- availability over two weeks with a cold cache.
    proxy_connect_timeout_seconds: float = 1.0
    proxy_read_timeout_seconds: float = 10.0
    proxy_write_timeout_seconds: float = 10.0
    proxy_pool_timeout_seconds: float = 1.0
