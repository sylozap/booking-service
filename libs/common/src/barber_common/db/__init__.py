"""Database access shared by the services that own a schema."""

from barber_common.db.base import NAMING_CONVENTION, Base, metadata
from barber_common.db.engine import create_engine
from barber_common.db.errors import (
    SQLSTATE_CHECK_VIOLATION,
    SQLSTATE_EXCLUSION_VIOLATION,
    SQLSTATE_FOREIGN_KEY_VIOLATION,
    SQLSTATE_UNIQUE_VIOLATION,
    constraint_name_of,
    is_unique_violation,
    sqlstate_of,
)
from barber_common.db.migrations import (
    SchemaVersionMismatch,
    check_schema_is_current,
    create_shared_tables,
    drop_shared_tables,
    load_config,
)
from barber_common.db.session import (
    Database,
    after_commit,
    create_session_factory,
    get_session,
    transaction,
    unit_of_work,
)

__all__ = [
    "NAMING_CONVENTION",
    "SQLSTATE_CHECK_VIOLATION",
    "SQLSTATE_EXCLUSION_VIOLATION",
    "SQLSTATE_FOREIGN_KEY_VIOLATION",
    "SQLSTATE_UNIQUE_VIOLATION",
    "Base",
    "Database",
    "SchemaVersionMismatch",
    "after_commit",
    "check_schema_is_current",
    "constraint_name_of",
    "create_engine",
    "create_session_factory",
    "create_shared_tables",
    "drop_shared_tables",
    "get_session",
    "is_unique_violation",
    "load_config",
    "metadata",
    "sqlstate_of",
    "transaction",
    "unit_of_work",
]
