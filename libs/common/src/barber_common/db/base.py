"""Declarative base shared by every service that owns tables.

The naming convention is not cosmetic: without it Alembic invents names for
indexes and constraints, and a downgrade that has to drop one of them becomes
manual work against the production schema.
"""

from __future__ import annotations

import datetime
import decimal

from sqlalchemy import MetaData, Numeric
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import DeclarativeBase

__all__ = ["NAMING_CONVENTION", "Base", "metadata"]

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    """Root of the ORM models.

    Two project-wide rules are encoded here:

    * every ``datetime`` column is ``timestamptz``; a naive timestamp in the
      database makes a daylight-saving transition ambiguous;
    * money is ``numeric``, never a floating point type.

    Relationships are declared with ``lazy="raise"`` at the point of definition:
    SQLAlchemy has no global default for it, and a lazy load that slips through
    turns into an N+1 that only shows up under load.

    ``eager_defaults`` is the third, and it is not a performance setting. A
    column filled by the database -- ``created_at``, ``updated_at`` and their
    ``onupdate`` -- leaves the attribute expired after a flush, and the next
    read of it is implicit IO. Under asyncio there is no such thing: SQLAlchemy
    raises ``MissingGreenlet`` rather than blocking, so a scenario mapping a
    just-updated row into a response fails at the moment it reads the
    timestamp. With this on, the value comes back through ``RETURNING`` on the
    statement that wrote it -- no expiry, no second round trip, and the same
    behaviour for an ``INSERT`` and an ``UPDATE``.
    """

    metadata = metadata

    __mapper_args__ = {"eager_defaults": True}

    type_annotation_map = {
        datetime.datetime: TIMESTAMP(timezone=True),
        decimal.Decimal: Numeric(10, 2),
    }
