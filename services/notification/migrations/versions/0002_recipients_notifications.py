"""recipients and the journal of notifications

``recipients`` is the copy of contacts filled from ``auth.users.v1``.
``notifications`` is the journal of what was sent and, while a row is pending,
the queue of the delivery worker. ``UNIQUE(dedup_key)`` is the guarantee that
an event delivered twice is sent once: the database refuses the second row,
not a check in the code.

Revision ID: 0002_recipients_notifications
Revises: 0001_shared_tables
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_recipients_notifications"
down_revision: str | None = "0001_shared_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column[object]]:
    return [
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "recipients",
        # Not a foreign key: accounts live in auth.
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("email_confirmed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "preferences",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("contacts_updated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("user_id", name="pk_recipients"),
    )

    op.create_table(
        "notifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("template", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("dedup_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "next_attempt_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        *_timestamps(),
        sa.Column("sent_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("channel IN ('email', 'telegram')", name="channel_known"),
        sa.CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'failed')", name="status_known"
        ),
        sa.CheckConstraint("attempts >= 0", name="attempts_not_negative"),
        sa.PrimaryKeyConstraint("id", name="pk_notifications"),
        sa.UniqueConstraint("dedup_key", name="uq_notifications_dedup_key"),
    )
    op.create_index(
        "ix_notifications_next_attempt_at",
        "notifications",
        ["next_attempt_at"],
        postgresql_where=sa.text("status IN ('pending', 'sending')"),
    )
    op.create_index(
        "ix_notifications_user_id_created_at",
        "notifications",
        ["user_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_user_id_created_at", table_name="notifications")
    op.drop_index("ix_notifications_next_attempt_at", table_name="notifications")
    op.drop_table("notifications")
    op.drop_table("recipients")
