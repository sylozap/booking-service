"""Database access shared by the services that own a schema."""

from barber_common.db.base import NAMING_CONVENTION, Base, metadata
from barber_common.db.engine import create_engine
from barber_common.db.migrations import (
    SchemaVersionMismatch,
    check_schema_is_current,
    create_shared_tables,
    drop_shared_tables,
    load_config,
)
from barber_common.db.session import (
    Database,
    create_session_factory,
    get_session,
    transaction,
    unit_of_work,
)

__all__ = [
    "NAMING_CONVENTION",
    "Base",
    "Database",
    "SchemaVersionMismatch",
    "check_schema_is_current",
    "create_engine",
    "create_session_factory",
    "create_shared_tables",
    "drop_shared_tables",
    "get_session",
    "load_config",
    "metadata",
    "transaction",
    "unit_of_work",
]
