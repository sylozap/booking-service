"""idempotency keys

Revision ID: 0002_idempotency_keys
Revises: 0001_shared_tables
"""

from barber_common.db.migrations import create_idempotency_keys, drop_idempotency_keys

revision = "0002_idempotency_keys"
down_revision = "0001_shared_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    create_idempotency_keys()


def downgrade() -> None:
    drop_idempotency_keys()
