"""auth tables: users, roles, tokens, signing keys and service clients

The whole schema of the auth database in one revision. It is one logical
change -- "auth owns accounts and their credentials" -- and the tables of
stages T1.5 to T1.10 are created here as well: splitting them across five
revisions would make every one of them a migration nobody can apply alone.

Two shapes deserve a word.

``users`` is unique on ``lower(email)`` rather than on ``email``: the same
address in two cases is one person, and a plain unique constraint would let
them register twice. The column stores the normalised value anyway; the
functional index is the guarantee that survives a write path that forgets to.

``user_roles`` carries a surrogate key. docs/05-data-model.md describes it as
``PK(user_id, role, salon_id)``, and PostgreSQL does not allow a null inside a
primary key, while a global role has no salon. The identity is therefore two
partial unique indexes -- one over the global grants, one over the scoped ones
-- which forbids the same grant twice and still lets a user be a global
``master`` and the ``salon_admin`` of one salon at the same time.

Revision ID: 0002_auth_tables
Revises: 0001_shared_tables
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import INET

revision: str = "0002_auth_tables"
down_revision: str | None = "0001_shared_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLES = ("client", "master", "salon_admin", "super_admin")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("phone", sa.String(length=16), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("email_confirmed_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_users"),
    )
    op.create_index("uq_users_lower_email", "users", [sa.text("lower(email)")], unique=True)
    op.create_index("uq_users_phone", "users", ["phone"], unique=True)

    op.create_table(
        "user_roles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        # No foreign key: salons live in catalog, and this database may not
        # constrain another service's table.
        sa.Column("salon_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # The bare name, not the final one: the naming convention of
        # barber_common.db.base turns it into ck_user_roles_role, and spelling
        # the prefix out here would produce ck_user_roles_ck_user_roles_role.
        sa.CheckConstraint(
            f"role IN ({', '.join(repr(role) for role in ROLES)})",
            name="role",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_user_roles_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_roles"),
    )
    op.create_index(
        "uq_user_roles_user_id_role_global",
        "user_roles",
        ["user_id", "role"],
        unique=True,
        postgresql_where=sa.text("salon_id IS NULL"),
    )
    op.create_index(
        "uq_user_roles_user_id_role_salon_id",
        "user_roles",
        ["user_id", "role", "salon_id"],
        unique=True,
        postgresql_where=sa.text("salon_id IS NOT NULL"),
    )
    op.create_index("ix_user_roles_user_id", "user_roles", ["user_id"])

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("replaced_by", sa.Uuid(), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.Column("ip", INET(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_refresh_tokens_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_refresh_tokens"),
    )
    op.create_index("uq_refresh_tokens_token_hash", "refresh_tokens", ["token_hash"], unique=True)
    op.create_index(
        "ix_refresh_tokens_user_id_revoked_at", "refresh_tokens", ["user_id", "revoked_at"]
    )

    op.create_table(
        "email_confirmations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_email_confirmations_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_email_confirmations"),
    )
    op.create_index(
        "uq_email_confirmations_token_hash", "email_confirmations", ["token_hash"], unique=True
    )
    op.create_index("ix_email_confirmations_user_id", "email_confirmations", ["user_id"])

    op.create_table(
        "signing_keys",
        sa.Column("kid", sa.String(length=64), nullable=False),
        sa.Column("public_pem", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("kid", name="pk_signing_keys"),
    )
    op.create_index(
        "ix_signing_keys_is_active",
        "signing_keys",
        ["is_active"],
        postgresql_where=sa.text("is_active"),
    )

    op.create_table(
        "service_clients",
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("secret_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "scopes",
            sa.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("client_id", name="pk_service_clients"),
    )


def downgrade() -> None:
    op.drop_table("service_clients")

    op.drop_index("ix_signing_keys_is_active", table_name="signing_keys")
    op.drop_table("signing_keys")

    op.drop_index("ix_email_confirmations_user_id", table_name="email_confirmations")
    op.drop_index("uq_email_confirmations_token_hash", table_name="email_confirmations")
    op.drop_table("email_confirmations")

    op.drop_index("ix_refresh_tokens_user_id_revoked_at", table_name="refresh_tokens")
    op.drop_index("uq_refresh_tokens_token_hash", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")

    op.drop_index("ix_user_roles_user_id", table_name="user_roles")
    op.drop_index("uq_user_roles_user_id_role_salon_id", table_name="user_roles")
    op.drop_index("uq_user_roles_user_id_role_global", table_name="user_roles")
    op.drop_table("user_roles")

    op.drop_index("uq_users_phone", table_name="users")
    op.drop_index("uq_users_lower_email", table_name="users")
    op.drop_table("users")
