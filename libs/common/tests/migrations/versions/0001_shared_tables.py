"""shared tables: outbox and processed_events

Revision ID: 0001_shared_tables
Revises:
"""

from barber_common.db.migrations import create_shared_tables, drop_shared_tables

revision = "0001_shared_tables"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    create_shared_tables()


def downgrade() -> None:
    drop_shared_tables()
