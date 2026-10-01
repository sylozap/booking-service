"""Settings of the api-gateway service."""

from __future__ import annotations

from pydantic import Field

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

    # --- rate limiting -----------------------------------------------------
    # docs/04-api-contracts.md, "Rate limiting". A strict limit is counted on
    # top of the general one, not instead of it.
    rate_limit_anonymous_per_minute: int = 30
    rate_limit_user_per_minute: int = 120
    rate_limit_booking_create_per_minute: int = 5
    rate_limit_registration_per_hour: int = 3
    # A limiter that has gone slow must not slow every request down with it;
    # past this deadline the request is let through, as when Redis is down.
    rate_limit_timeout_seconds: float = 0.2
    # Proxies in front of the gateway whose X-Forwarded-For entries are
    # believed: 0 when clients connect directly (the local stack), 1 behind the
    # Ingress of the cluster. More than there really are lets a client choose
    # the address it is limited by.
    trusted_proxy_hops: int = 0

    # --- the card of a master ----------------------------------------------
    # How many days of free starts are read, and how many of the nearest are
    # shown. The window cannot exceed what availability answers, two weeks.
    card_slots_window_days: int = Field(default=7, ge=1, le=13)
    card_slots_limit: int = Field(default=10, ge=1)
