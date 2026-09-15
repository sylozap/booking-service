"""shared tables: outbox and processed_events

Creates the shared tables from the DDL in the chassis.

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
