"""catalog tables: salons, masters, services and the master-service link

The whole schema of the catalog database in one revision.

``salons`` carries the four booking policies with database defaults, applied
by ``booking``. ``masters.user_id`` names an account in ``auth`` without a
foreign key. ``master_services`` uses the pair of ids as its primary key.

Revision ID: 0002_catalog_tables
Revises: 0001_shared_tables
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_catalog_tables"
down_revision: str | None = "0001_shared_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Default booking policies of a salon.
DEFAULT_SLOT_STEP_MIN = 15
DEFAULT_BOOKING_MIN_LEAD_MIN = 120
DEFAULT_BOOKING_HORIZON_DAYS = 60
DEFAULT_CANCEL_DEADLINE_MIN = 240


def upgrade() -> None:
    op.create_table(
        "salons",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("address", sa.String(length=500), nullable=False),
        sa.Column("city", sa.String(length=120), nullable=False),
        sa.Column("phone", sa.String(length=16), nullable=False),
        # An IANA identifier, never an offset: an offset is right for half the
        # year and moves every schedule by an hour on the day the clocks change.
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column(
            "slot_step_min",
            sa.Integer(),
            server_default=sa.text(str(DEFAULT_SLOT_STEP_MIN)),
            nullable=False,
        ),
        sa.Column(
            "booking_min_lead_min",
            sa.Integer(),
            server_default=sa.text(str(DEFAULT_BOOKING_MIN_LEAD_MIN)),
            nullable=False,
        ),
        sa.Column(
            "booking_horizon_days",
            sa.Integer(),
            server_default=sa.text(str(DEFAULT_BOOKING_HORIZON_DAYS)),
            nullable=False,
        ),
        sa.Column(
            "cancel_deadline_min",
            sa.Integer(),
            server_default=sa.text(str(DEFAULT_CANCEL_DEADLINE_MIN)),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
        # The bare names, not the final ones: the naming convention of
        # barber_common.db.base turns each into ck_salons_<name>, and spelling
        # the prefix out here would produce ck_salons_ck_salons_<name>.
        sa.CheckConstraint("slot_step_min > 0", name="slot_step_min_positive"),
        sa.CheckConstraint("booking_min_lead_min >= 0", name="booking_min_lead_min_not_negative"),
        sa.CheckConstraint("booking_horizon_days > 0", name="booking_horizon_days_positive"),
        sa.CheckConstraint("cancel_deadline_min >= 0", name="cancel_deadline_min_not_negative"),
        sa.PrimaryKeyConstraint("id", name="pk_salons"),
    )
    op.create_index("ix_salons_city_name_id", "salons", ["city", "name", "id"])

    op.create_table(
        "masters",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("salon_id", sa.Uuid(), nullable=False),
        # No foreign key: accounts live in auth, and this database may not
        # constrain another service's table.
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("bio", sa.Text(), nullable=True),
        sa.Column("photo_url", sa.String(length=1000), nullable=True),
        sa.Column("specialization", sa.String(length=200), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["salon_id"],
            ["salons.id"],
            name="fk_masters_salon_id_salons",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_masters"),
    )
    op.create_index("uq_masters_user_id_salon_id", "masters", ["user_id", "salon_id"], unique=True)
    op.create_index("ix_masters_salon_id_is_active", "masters", ["salon_id", "is_active"])

    op.create_table(
        "services",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("salon_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("base_duration_min", sa.Integer(), nullable=False),
        sa.Column("base_price", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("is_archived", sa.Boolean(), server_default=sa.text("false"), nullable=False),
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
        sa.CheckConstraint("base_duration_min > 0", name="base_duration_min_positive"),
        sa.CheckConstraint("base_price >= 0", name="base_price_not_negative"),
        sa.ForeignKeyConstraint(
            ["salon_id"],
            ["salons.id"],
            name="fk_services_salon_id_salons",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_services"),
    )
    op.create_index(
        "ix_services_salon_id_is_archived_name_id",
        "services",
        ["salon_id", "is_archived", "name", "id"],
    )

    op.create_table(
        "master_services",
        sa.Column("master_id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("price_override", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("duration_override", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
        sa.CheckConstraint(
            "duration_override IS NULL OR duration_override > 0",
            name="duration_override_positive",
        ),
        sa.CheckConstraint(
            "price_override IS NULL OR price_override >= 0",
            name="price_override_not_negative",
        ),
        sa.ForeignKeyConstraint(
            ["master_id"],
            ["masters.id"],
            name="fk_master_services_master_id_masters",
            ondelete="CASCADE",
        ),
        # No cascade on purpose: a service is never deleted, and a cascade here
        # would be a promise about something that must not happen.
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
            name="fk_master_services_service_id_services",
        ),
        sa.PrimaryKeyConstraint("master_id", "service_id", name="pk_master_services"),
    )


def downgrade() -> None:
    op.drop_table("master_services")
    op.drop_index("ix_services_salon_id_is_archived_name_id", table_name="services")
    op.drop_table("services")
    op.drop_index("ix_masters_salon_id_is_active", table_name="masters")
    op.drop_index("uq_masters_user_id_salon_id", table_name="masters")
    op.drop_table("masters")
    op.drop_index("ix_salons_city_name_id", table_name="salons")
    op.drop_table("salons")
