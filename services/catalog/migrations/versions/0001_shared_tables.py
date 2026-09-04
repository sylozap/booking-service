"""shared tables: outbox and processed_events

The reusable revision of T0.14. The DDL lives in the chassis, so the catalog
database gets exactly the same two tables as every other database that
publishes or consumes events.

Revision ID: 0001_shared_tables
Revises:
"""

from collections.abc import Sequence

from barber_common.db.migrations import create_shared_tables, drop_shared_tables

revision: str = "0001_shared_tables"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    create_shared_tables()


def downgrade() -> None:
    drop_shared_tables()
