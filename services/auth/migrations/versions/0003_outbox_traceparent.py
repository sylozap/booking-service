"""outbox traceparent: the trace an event was written in

The relay publishes in that context, so the consumer continues the trace of
the request instead of starting a new one. The DDL lives in the chassis.

Revision ID: 0003_outbox_traceparent
Revises: 0002_auth_tables
"""

from collections.abc import Sequence

from barber_common.db.migrations import add_outbox_traceparent, drop_outbox_traceparent

revision: str = "0003_outbox_traceparent"
down_revision: str | None = "0002_auth_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    add_outbox_traceparent()


def downgrade() -> None:
    drop_outbox_traceparent()
