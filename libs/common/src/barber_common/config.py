"""Application settings shared by every service.

Settings come from environment variables only; reading ``os.environ`` directly
anywhere else is forbidden. Every service subclasses :class:`BaseAppSettings`
and adds its own fields.

Secrets, database and broker addresses have no defaults on purpose: a process
that starts with an incomplete configuration becomes an incident a week later.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import PostgresDsn, RedisDsn, Secret, UrlConstraints, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "BaseAppSettings",
    "ConfigurationError",
    "Environment",
    "LogLevel",
    "SecretPostgresDsn",
    "SecretRedisDsn",
]


class Environment(StrEnum):
    """Deployment environment, reported in every log record and metric."""

    LOCAL = "local"
    TEST = "test"
    DEV = "dev"
    PROD = "prod"


class LogLevel(StrEnum):
    """Threshold of the root logger."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


AsyncPostgresDsn = Annotated[
    PostgresDsn,
    UrlConstraints(host_required=True, allowed_schemes=["postgresql+asyncpg"]),
]

# A DSN carries a password, so it is kept behind Secret: str() and repr() of the
# settings object then show a mask instead of the credentials.
SecretPostgresDsn = Secret[AsyncPostgresDsn]
SecretRedisDsn = Secret[RedisDsn]


class ConfigurationError(RuntimeError):
    """Raised at startup when the environment does not describe a runnable service."""


class BaseAppSettings(BaseSettings):
    """Settings every service has.

    Field ``database_dsn`` becomes environment variable ``DATABASE_DSN``,
    ``kafka_bootstrap_servers`` becomes ``KAFKA_BOOTSTRAP_SERVERS``, and so on.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    service_name: str
    environment: Environment
    log_level: LogLevel = LogLevel.INFO

    database_dsn: SecretPostgresDsn
    database_pool_size: int = 10
    database_max_overflow: int = 5
    database_pool_timeout_seconds: float = 5.0
    database_echo: bool = False

    redis_dsn: SecretRedisDsn

    kafka_bootstrap_servers: str

    # --- cache --------------------------------------------------------------
    # The cache accelerates reads and decides nothing (ADR-0012), so a
    # deployment may run without it and the switch is a supported state rather
    # than a debugging aid. Five minutes is the window
    # docs/IMPLEMENTATION_PLAN.md T2.7 fixes: long enough to be worth having,
    # short enough that the race every cache-aside scheme has -- a reader that
    # started before a write storing what it read after it -- cannot outlive a
    # coffee break.
    cache_enabled: bool = True
    cache_ttl_seconds: int = 300
    # A cache that has gone slow is worse than one that is gone: without this
    # the read it exists to accelerate would wait on it.
    cache_timeout_seconds: float = 0.2

    otlp_enabled: bool = False
    otlp_endpoint: str | None = None

    # --- verifying access tokens -------------------------------------------
    # Every service checks the tokens it receives itself (ADR-0010), so every
    # service needs these -- including ``auth``, which verifies with the key it
    # already holds instead of fetching it from itself.
    #
    # The expected ``iss``. A setting rather than a constant so that a token
    # minted by the auth of the dev cluster is not accepted in prod.
    jwt_issuer: str = "https://barber.local/auth"
    # Base URL of ``auth``, where /.well-known/jwks.json is served. Optional
    # only because ``auth`` does not use it; any other service that installs
    # the JWKS-backed verifier fails at startup without it.
    jwks_url: str | None = None
    # How long keys are kept before a refresh, and the floor on how often an
    # unknown kid may force one. The second is what keeps a burst of forged
    # headers from turning into a burst of requests to ``auth``.
    jwks_cache_ttl_seconds: float = 300.0
    jwks_refresh_min_interval_seconds: float = 10.0
    # For clock skew between pods, and for nothing else. Not a grace period:
    # an access token that cannot be revoked is only acceptable because it
    # expires when it says it does.
    jwt_leeway_seconds: float = 5.0

    health_check_timeout_seconds: float = 2.0

    @classmethod
    def load(cls) -> Self:
        """Build settings from the environment, failing with a readable message.

        Pydantic already names the offending field; this wrapper translates the
        field names into the environment variables an operator actually sets.
        """
        try:
            return cls()
        except ValidationError as error:
            raise ConfigurationError(cls._describe(error)) from error

    @classmethod
    def _describe(cls, error: ValidationError) -> str:
        lines = [f"invalid configuration for {cls.__name__}:"]
        for problem in error.errors():
            location = problem["loc"]
            variable = str(location[0]).upper() if location else "<unknown>"
            lines.append(f"  {variable}: {problem['msg']}")
        return "\n".join(lines)
