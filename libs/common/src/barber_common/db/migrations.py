"""Migrations: the shared tables and the schema version check at startup.

:func:`create_shared_tables` creates ``outbox`` and ``processed_events`` from
the first revision of every service. :func:`check_schema_is_current` refuses to
start the application when the database revision differs from the head of its
migration scripts.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.op import create_index, create_table, drop_index, drop_table
from alembic.script import ScriptDirectory
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncEngine

from barber_common.config import SecretPostgresDsn
from barber_common.logging import get_logger

__all__ = [
    "MigrationSettings",
    "SchemaVersionMismatch",
    "check_schema_is_current",
    "create_shared_tables",
    "current_revisions",
    "drop_shared_tables",
    "head_revisions",
    "load_config",
]

_logger = get_logger(__name__)


class SchemaVersionMismatch(RuntimeError):
    """The database schema is not the one this code was written against."""


class MigrationSettings(BaseSettings):
    """The one setting a migration run needs.

    Separate from :class:`~barber_common.config.BaseAppSettings`, so a migration
    run does not require Redis, Kafka or service settings.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    database_dsn: SecretPostgresDsn

    @property
    def dsn(self) -> str:
        return str(self.database_dsn.get_secret_value())


def create_shared_tables() -> None:
    """Create ``outbox`` and ``processed_events``.

    Called from the first revision of every service that publishes or consumes
    events. Constraint names follow the naming convention of
    ``barber_common.db.base``.
    """
    create_table(
        "outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("topic", sa.String(length=255), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("event_version", sa.Integer(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column("causation_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_outbox"),
    )
    # Partial: the relay only ever reads unpublished rows, while the table
    # keeps the published ones until they are pruned. A full index would grow
    # with the history and slow down the hot path for nothing.
    create_index(
        "ix_outbox_created_at",
        "outbox",
        ["created_at"],
        postgresql_where=sa.text("published_at IS NULL"),
    )

    create_table(
        "processed_events",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("consumer_group", sa.String(length=128), nullable=False),
        sa.Column(
            "processed_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # The pair, not the id: two groups read the same topic and each of them
        # has to see the event once.
        sa.PrimaryKeyConstraint("event_id", "consumer_group", name="pk_processed_events"),
    )


def drop_shared_tables() -> None:
    """Undo :func:`create_shared_tables`, in the reverse order."""
    drop_table("processed_events")
    drop_index("ix_outbox_created_at", table_name="outbox")
    drop_table("outbox")


def load_config(alembic_ini: Path | str, *, dsn: str | None = None) -> Config:
    """Read ``alembic.ini`` of a service.

    ``script_location`` in the file is relative to it, so the path is made
    absolute here: the migration Job and the tests run from other directories
    than the service root.
    """
    path = Path(alembic_ini).resolve()
    config = Config(str(path))
    script_location = config.get_main_option("script_location")
    if script_location is not None and not Path(script_location).is_absolute():
        config.set_main_option("script_location", str(path.parent / script_location))
    if dsn is not None:
        config.set_main_option("sqlalchemy.url", dsn)
    return config


def head_revisions(config: Config) -> set[str]:
    """Revisions the migration scripts of this service end at."""
    return set(ScriptDirectory.from_config(config).get_heads())


async def current_revisions(engine: AsyncEngine) -> set[str]:
    """Revisions the database is at. Empty when nothing has been applied."""
    async with engine.connect() as connection:
        return await connection.run_sync(_read_revisions)


def _read_revisions(connection: sa.Connection) -> set[str]:
    return set(MigrationContext.configure(connection).get_current_heads())


async def check_schema_is_current(engine: AsyncEngine, config: Config) -> None:
    """Fail the startup when the schema is not what the code expects.

    Raising is the point. Serving requests against a schema of the wrong
    version corrupts data quietly, while a pod that refuses to start is a
    failed rollout an operator sees within a minute.
    """
    expected = head_revisions(config)
    actual = await current_revisions(engine)
    if actual == expected:
        _logger.info("database schema is current", revision=sorted(actual))
        return

    raise SchemaVersionMismatch(
        "database schema does not match the code: "
        f"migrations end at {sorted(expected) or ['<none>']}, "
        f"the database is at {sorted(actual) or ['<none>']}. "
        "Run 'alembic upgrade head' before starting the service."
    )
