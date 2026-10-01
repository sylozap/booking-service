"""the topic of the event behind a notification

A notification the worker gives up on is published to the dead letter topic of
the event that caused it, and the journal is the only place left that knows
which topic that was.

Rows queued before the column existed get it from their template: the
confirmation letter came from auth, everything else from booking. The column
is filled before it turns NOT NULL, so the upgrade works on a journal that is
not empty.

Revision ID: 0005_notification_topic
Revises: 0004_notification_address
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_notification_topic"
down_revision: str | None = "0004_notification_address"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("notifications", sa.Column("topic", sa.String(length=255), nullable=True))
    op.execute(
        "UPDATE notifications SET topic = CASE template "
        "WHEN 'email_confirmation' THEN 'auth.users.v1' "
        "ELSE 'booking.bookings.v1' END"
    )
    op.alter_column("notifications", "topic", nullable=False)


def downgrade() -> None:
    op.drop_column("notifications", "topic")
