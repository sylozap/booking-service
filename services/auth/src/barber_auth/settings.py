"""Settings of the auth service."""

from __future__ import annotations

from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, SecretStr, field_validator, model_validator

from barber_auth.domain.tokens import (
    ACCESS_TOKEN_TTL_MINUTES,
    REFRESH_TOKEN_TTL_DAYS,
    SERVICE_TOKEN_TTL_MINUTES,
)
from barber_common.config import BaseAppSettings, ConfigurationError

__all__ = ["ALEMBIC_INI", "AuthSettings", "ServiceClientConfig"]

# services/auth/alembic.ini, two directories above the package. The startup
# check and the migration Job read the same file, so they cannot disagree about
# which revision is the head.
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


class ServiceClientConfig(BaseModel):
    """One machine-to-machine client, as the environment describes it.

    The secret is a ``SecretStr`` and reaches the database only as a hash.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    secret: SecretStr
    # What a token issued to this client is allowed to do. Deliberately not
    # roles: a service is not a person and has no place in a salon.
    scopes: tuple[str, ...] = ()


class AuthSettings(BaseAppSettings):
    """Everything the chassis needs, plus what only this service has."""

    service_name: str = "auth"

    # --- passwords ---------------------------------------------------------
    # argon2 cost. Defaults follow the second recommended option of RFC 9106:
    # 64 MiB and three passes. Stored hashes keep their own parameters.
    password_argon2_time_cost: int = 3
    password_argon2_memory_kib: int = 65536
    password_argon2_parallelism: int = 4
    password_argon2_hash_length: int = 32
    password_argon2_salt_length: int = 16
    password_min_length: int = 10

    # --- email confirmation ------------------------------------------------
    # The confirmation link is valid for a day.
    email_confirmation_ttl_hours: int = 24
    # Where the link in the letter points. The frontend page that reads the
    # token out of the query string and posts it to /api/v1/auth/confirm-email.
    email_confirmation_url: str = "http://localhost:8080/confirm-email"

    # --- signing keys ------------------------------------------------------
    # The RS256 private key, given either as a file path (a mounted Kubernetes
    # Secret) or as the PEM itself, never both. There is no default.
    jwt_private_key_path: Path | None = None
    jwt_private_key: SecretStr | None = None

    # ``jwt_issuer`` is on BaseAppSettings: auth writes the claim and every
    # service checks it, so one setting has to be readable from both sides.

    # --- token lifetimes ---------------------------------------------------
    # Access tokens cannot be revoked, so they are short lived; a refresh token
    # keeps a session for thirty days.
    access_token_ttl_minutes: int = ACCESS_TOKEN_TTL_MINUTES
    refresh_token_ttl_days: int = REFRESH_TOKEN_TTL_DAYS
    service_token_ttl_minutes: int = SERVICE_TOKEN_TTL_MINUTES

    # --- background cleanup ------------------------------------------------
    # How long a row is kept after it expires, so recent incidents can still be
    # investigated.
    cleanup_interval_seconds: float = 3600.0
    cleanup_batch_size: int = 1000
    cleanup_token_retention_days: int = 7
    cleanup_confirmation_retention_days: int = 7
    cleanup_processed_event_retention_days: int = 7

    # --- machine to machine clients ----------------------------------------
    # Registered at startup. Given as JSON, for example
    # {"booking": {"secret": "...", "scopes": ["catalog:read"]}}
    # Empty by default, in which case the token endpoint refuses everything.
    service_clients: dict[str, ServiceClientConfig] = {}

    @field_validator("jwt_private_key_path", "jwt_private_key", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        """Treat an empty variable as an absent one.

        Lets ``.env.example`` list both key sources with one of them left empty.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _exactly_one_signing_key_source(self) -> Self:
        """Refuse a configuration that names no key, or names two.

        Two sources would mean the answer to "which key signs the tokens"
        depends on the order the fields are read in, and that is the kind of
        question an operator should never have to ask during an incident.
        """
        provided = [self.jwt_private_key_path is not None, self.jwt_private_key is not None]
        if not any(provided):
            raise ValueError(
                "set either JWT_PRIVATE_KEY_PATH or JWT_PRIVATE_KEY: "
                "auth cannot issue access tokens without a signing key"
            )
        if all(provided):
            raise ValueError(
                "JWT_PRIVATE_KEY_PATH and JWT_PRIVATE_KEY are both set; keep exactly one"
            )
        return self

    def signing_key_pem(self) -> str:
        """The private key, read from wherever this deployment keeps it.

        Read when the signer is built, never stored in the settings or logged.
        """
        if self.jwt_private_key is not None:
            # An inline PEM carries escaped newlines, which are restored here.
            return self.jwt_private_key.get_secret_value().replace("\\n", "\n")

        # Guaranteed by the validator above, restated for mypy and for anyone
        # reading this method on its own.
        if self.jwt_private_key_path is None:  # pragma: no cover - validator forbids it
            raise ConfigurationError("no signing key is configured")

        try:
            return self.jwt_private_key_path.read_text(encoding="utf-8")
        except OSError as error:
            raise ConfigurationError(
                f"signing key at {self.jwt_private_key_path} cannot be read: {error}"
            ) from error
