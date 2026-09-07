"""Settings of the auth service."""

from __future__ import annotations

from pathlib import Path
from typing import Self

from pydantic import SecretStr, field_validator, model_validator

from barber_auth.domain.tokens import ACCESS_TOKEN_TTL_MINUTES, REFRESH_TOKEN_TTL_DAYS
from barber_common.config import BaseAppSettings, ConfigurationError

__all__ = ["ALEMBIC_INI", "AuthSettings"]

# services/auth/alembic.ini, two directories above the package. The startup
# check and the migration Job read the same file, so they cannot disagree about
# which revision is the head.
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


class AuthSettings(BaseAppSettings):
    """Everything the chassis needs, plus what only this service has."""

    service_name: str = "auth"

    # --- passwords ---------------------------------------------------------
    # The cost of one hash. Defaults follow the "second recommended option" of
    # RFC 9106: 64 MiB and three passes, which is the memory-hard side of the
    # trade-off a request path can still afford. Raising them later does not
    # invalidate the stored hashes -- each one carries the parameters it was
    # made with.
    password_argon2_time_cost: int = 3
    password_argon2_memory_kib: int = 65536
    password_argon2_parallelism: int = 4
    password_argon2_hash_length: int = 32
    password_argon2_salt_length: int = 16
    password_min_length: int = 10

    # --- email confirmation ------------------------------------------------
    # docs/IMPLEMENTATION_PLAN.md T1.4: the link is valid for a day.
    email_confirmation_ttl_hours: int = 24
    # Where the link in the letter points. The frontend page that reads the
    # token out of the query string and posts it to /api/v1/auth/confirm-email.
    email_confirmation_url: str = "http://localhost:8080/confirm-email"

    # --- signing keys ------------------------------------------------------
    # The RS256 private key the access tokens are signed with. It arrives one
    # of two ways and never through both: a path, which is how a Kubernetes
    # Secret is mounted, or the PEM itself, which is what compose and a local
    # .env can carry. Neither has a default -- a service that invents its own
    # signing key issues tokens nobody else can verify.
    jwt_private_key_path: Path | None = None
    jwt_private_key: SecretStr | None = None

    # ``jwt_issuer`` is on BaseAppSettings: auth writes the claim and every
    # service checks it, so one setting has to be readable from both sides.

    # --- token lifetimes ---------------------------------------------------
    # Fifteen minutes is what makes a token that cannot be revoked acceptable
    # (ADR-0010); thirty days is how long a session survives without a login.
    access_token_ttl_minutes: int = ACCESS_TOKEN_TTL_MINUTES
    refresh_token_ttl_days: int = REFRESH_TOKEN_TTL_DAYS

    @field_validator("jwt_private_key_path", "jwt_private_key", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        """Treat an empty variable as an absent one.

        ``.env.example`` lists every variable, including the ones a given
        deployment leaves empty, and an empty string is what pydantic would
        otherwise turn into ``Path("")``. Without this the file cannot be
        copied and edited, because both key sources would look set at once.
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

        A method and not a field: the file is read when the signer is built, so
        a rotation that replaces the mounted Secret takes effect on the next
        restart rather than being frozen into the settings object at import
        time. The value is returned and immediately handed to the signer; it is
        never logged, never put back into the settings and never stored.
        """
        if self.jwt_private_key is not None:
            # A PEM is several lines and an environment variable is one, so the
            # inline form carries backslash-n and is unescaped here. A value
            # that already has real newlines -- a here-document in compose, a
            # multi-line .env entry -- passes through untouched.
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
