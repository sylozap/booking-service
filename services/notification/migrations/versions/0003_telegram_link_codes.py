"""one-time codes for linking a Telegram chat

A user asks for a code, sends it to the bot, and the chat the message came from
becomes their Telegram address. Only the SHA-256 of a code is stored.

Revision ID: 0003_telegram_link_codes
Revises: 0002_recipients_notifications
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_telegram_link_codes"
down_revision: str | None = "0002_recipients_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "telegram_link_codes",
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("code_hash", name="pk_telegram_link_codes"),
    )
    op.create_index("ix_telegram_link_codes_user_id", "telegram_link_codes", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_telegram_link_codes_user_id", table_name="telegram_link_codes")
    op.drop_table("telegram_link_codes")
