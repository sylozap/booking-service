"""Settings of the auth service."""

from __future__ import annotations

from pathlib import Path

from barber_common.config import BaseAppSettings

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
