"""the trace and the correlation id of the event behind a notification

The worker sends later, outside the consumer that queued the row; with these
two columns the send continues the trace of the request and its log records
carry its correlation id. Nullable for good: rows queued while tracing is off,
or before the columns existed, have neither.

Revision ID: 0007_notification_trace_context
Revises: 0006_outbox_traceparent
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_notification_trace_context"
down_revision: str | None = "0006_outbox_traceparent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "notifications", sa.Column("traceparent", sa.String(length=128), nullable=True)
    )
    op.add_column(
        "notifications", sa.Column("correlation_id", sa.String(length=64), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("notifications", "correlation_id")
    op.drop_column("notifications", "traceparent")
