"""booking tables: master settings, schedules and bookings

The schema of the booking database. ``btree_gist`` comes first: the exclusion
constraint mixes ``=`` on a uuid with ``&&`` on a range, and gist has no
operator class for the uuid without it.

``occupied_range`` is an ordinary column with a ``CHECK`` rather than a
generated one: ``timestamptz + interval`` is not immutable, and PostgreSQL
refuses it in a generation expression.

The active statuses are spelled out in the exclusion constraint. Adding a
status means changing the constraint in a migration of its own.

Revision ID: 0002_booking_tables
Revises: 0001_shared_tables
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_booking_tables"
down_revision: str | None = "0001_shared_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTIVE_STATUSES = "'pending', 'confirmed', 'completed', 'no_show'"
ALL_STATUSES = f"{ACTIVE_STATUSES}, 'cancelled_by_client', 'cancelled_by_salon'"


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
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    op.create_table(
        "master_settings",
        sa.Column("master_id", sa.Uuid(), nullable=False),
        # No foreign keys: salons, masters and accounts live in other services.
        sa.Column("salon_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("buffer_after_min", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("buffer_after_min >= 0", name="buffer_after_min_not_negative"),
        sa.PrimaryKeyConstraint("master_id", name="pk_master_settings"),
    )
    op.create_index("ix_master_settings_salon_id", "master_settings", ["salon_id"])

    op.create_table(
        "schedule_templates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("master_id", sa.Uuid(), nullable=False),
        sa.Column("weekday", sa.SmallInteger(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("weekday BETWEEN 0 AND 6", name="weekday_in_week"),
        sa.CheckConstraint("end_time > start_time", name="end_time_after_start_time"),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from", name="valid_to_not_before_valid_from"
        ),
        sa.ForeignKeyConstraint(
            ["master_id"],
            ["master_settings.master_id"],
            name="fk_schedule_templates_master_id_master_settings",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_schedule_templates"),
    )
    op.create_index(
        "ix_schedule_templates_master_id_weekday", "schedule_templates", ["master_id", "weekday"]
    )

    op.create_table(
        "schedule_exceptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("master_id", sa.Uuid(), nullable=False),
        sa.Column("effective_on", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=True),
        sa.Column("end_time", sa.Time(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('day_off', 'custom_hours', 'break')", name="kind_known"),
        sa.CheckConstraint(
            "(kind = 'day_off' AND start_time IS NULL AND end_time IS NULL)"
            " OR (kind <> 'day_off' AND start_time IS NOT NULL AND end_time > start_time)",
            name="times_match_kind",
        ),
        sa.ForeignKeyConstraint(
            ["master_id"],
            ["master_settings.master_id"],
            name="fk_schedule_exceptions_master_id_master_settings",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_schedule_exceptions"),
    )
    op.create_index(
        "ix_schedule_exceptions_master_id_effective_on",
        "schedule_exceptions",
        ["master_id", "effective_on"],
    )

    op.create_table(
        "bookings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("salon_id", sa.Uuid(), nullable=False),
        sa.Column("master_id", sa.Uuid(), nullable=False),
        sa.Column("client_user_id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("service_name", sa.Text(), nullable=False),
        sa.Column("price", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("duration_min", sa.Integer(), nullable=False),
        sa.Column("buffer_min", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("start_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("end_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("occupied_range", postgresql.TSTZRANGE(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("cancelled_by", sa.Uuid(), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column("cancelled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("reminder_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("reminder_sent_at", sa.TIMESTAMP(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("duration_min > 0", name="duration_min_positive"),
        sa.CheckConstraint("buffer_min >= 0", name="buffer_min_not_negative"),
        sa.CheckConstraint("end_at > start_at", name="end_at_after_start_at"),
        sa.CheckConstraint(
            "occupied_range = tstzrange(start_at, "
            "end_at + make_interval(mins => buffer_min), '[)')",
            name="occupied_range_matches_times",
        ),
        sa.CheckConstraint(f"status IN ({ALL_STATUSES})", name="status_known"),
        postgresql.ExcludeConstraint(
            (sa.column("master_id"), "="),
            (sa.column("occupied_range"), "&&"),
            name="ex_bookings_no_overlapping",
            using="gist",
            where=sa.text(f"status IN ({ACTIVE_STATUSES})"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_bookings"),
    )
    op.create_index("ix_bookings_master_id_start_at", "bookings", ["master_id", "start_at"])
    op.create_index(
        "ix_bookings_client_user_id_start_at",
        "bookings",
        ["client_user_id", sa.text("start_at DESC")],
    )
    op.create_index("ix_bookings_salon_id_start_at", "bookings", ["salon_id", "start_at"])
    op.create_index(
        "ix_bookings_reminder_at",
        "bookings",
        ["reminder_at"],
        postgresql_where=sa.text("reminder_sent_at IS NULL AND status IN ('pending', 'confirmed')"),
    )


def downgrade() -> None:
    op.drop_index("ix_bookings_reminder_at", table_name="bookings")
    op.drop_index("ix_bookings_salon_id_start_at", table_name="bookings")
    op.drop_index("ix_bookings_client_user_id_start_at", table_name="bookings")
    op.drop_index("ix_bookings_master_id_start_at", table_name="bookings")
    op.drop_table("bookings")
    op.drop_index("ix_schedule_exceptions_master_id_effective_on", table_name="schedule_exceptions")
    op.drop_table("schedule_exceptions")
    op.drop_index("ix_schedule_templates_master_id_weekday", table_name="schedule_templates")
    op.drop_table("schedule_templates")
    op.drop_index("ix_master_settings_salon_id", table_name="master_settings")
    op.drop_table("master_settings")
    # btree_gist stays: an extension is shared by the whole database, and
    # dropping it here could break an object this revision did not create.
