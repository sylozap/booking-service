"""outbox traceparent

Revision ID: 0003_outbox_traceparent
Revises: 0002_idempotency_keys
"""

from barber_common.db.migrations import add_outbox_traceparent, drop_outbox_traceparent

revision = "0003_outbox_traceparent"
down_revision = "0002_idempotency_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_outbox_traceparent()


def downgrade() -> None:
    drop_outbox_traceparent()
