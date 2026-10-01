"""an address named by the event itself

A confirmation letter goes to the address being confirmed, whatever the
recipient row says -- the row may not exist yet, or hold a newer address.
Such a notification carries its address; every other one leaves the column
empty and is sent where the recipient can be reached at the time of sending.

Revision ID: 0004_notification_address
Revises: 0003_telegram_link_codes
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_notification_address"
down_revision: str | None = "0003_telegram_link_codes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("notifications", sa.Column("address", sa.String(length=320), nullable=True))


def downgrade() -> None:
    op.drop_column("notifications", "address")
