"""Alembic environment of the auth service.

Everything easy to get wrong -- the async engine, ``compare_type``, the DSN
from the environment -- lives in the chassis, so this file only says which
metadata the migrations are compared against.
"""

from barber_common.db.alembic_env import run_migrations

from barber_auth.models import metadata

run_migrations(target_metadata=metadata)
