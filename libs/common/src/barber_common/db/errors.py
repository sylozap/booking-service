"""Telling one database failure from another.

``IntegrityError`` covers a unique violation, an exclusion violation, a failed
check and a broken foreign key alike, and treating them as one thing is how a
duplicate idempotency key gets reported as a taken slot. So the code and the
constraint are read explicitly, and each caller decides what its own SQLSTATE
means (docs/CODING_STANDARDS.md section 8).

Getting at them takes one indirection. The asyncpg dialect of SQLAlchemy
re-raises the driver exception as its own, copying ``sqlstate`` across but not
the name of the constraint, and keeps the original as ``__cause__``. That
detail is here so that no service has to know it.
"""

from __future__ import annotations

from sqlalchemy.exc import DBAPIError

__all__ = [
    "SQLSTATE_CHECK_VIOLATION",
    "SQLSTATE_EXCLUSION_VIOLATION",
    "SQLSTATE_FOREIGN_KEY_VIOLATION",
    "SQLSTATE_UNIQUE_VIOLATION",
    "constraint_name_of",
    "is_unique_violation",
    "sqlstate_of",
]

SQLSTATE_UNIQUE_VIOLATION = "23505"
# The main invariant of the platform arrives as this one (ADR-0002).
SQLSTATE_EXCLUSION_VIOLATION = "23P01"
SQLSTATE_FOREIGN_KEY_VIOLATION = "23503"
SQLSTATE_CHECK_VIOLATION = "23514"


def sqlstate_of(error: DBAPIError) -> str | None:
    """The five character code PostgreSQL answered with, if there is one."""
    return getattr(error.orig, "sqlstate", None)


def constraint_name_of(error: DBAPIError) -> str | None:
    """The constraint or index the statement violated, if the driver named it.

    The name is what turns "something is not unique" into "this email is taken":
    a table with two unique indexes cannot be told apart by the SQLSTATE alone.
    """
    dialect_error = error.orig
    if dialect_error is None:
        return None

    name = getattr(dialect_error, "constraint_name", None)
    if isinstance(name, str):
        return name

    # asyncpg through SQLAlchemy: the driver exception, which carries the name,
    # is the cause of the one the dialect raises.
    driver_error = dialect_error.__cause__
    name = getattr(driver_error, "constraint_name", None)
    return name if isinstance(name, str) else None


def is_unique_violation(error: DBAPIError, *, constraint: str | None = None) -> bool:
    """Whether this is a 23505, optionally from one named constraint."""
    if sqlstate_of(error) != SQLSTATE_UNIQUE_VIOLATION:
        return False
    return constraint is None or constraint_name_of(error) == constraint
