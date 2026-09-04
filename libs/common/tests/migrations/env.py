"""Environment of the chassis test migrations.

As short as the env.py of a real service: everything that matters -- the async
engine, compare_type, the DSN from the environment -- lives in the chassis.
"""

from barber_common.db.alembic_env import run_migrations
from barber_common.db.base import metadata

run_migrations(target_metadata=metadata)
