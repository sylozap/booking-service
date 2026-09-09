"""Salons: the shop window listing and the single row behind it."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Select, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from barber_catalog.domain.identifiers import SalonId
from barber_catalog.models.salon import Salon
from barber_common.pagination import PageRequest, cursor_uuid

__all__ = ["SALON_CURSOR_ARITY", "SalonRepository", "salon_cursor"]

# The listing is ordered by ``(name, id)``: alphabetical, because that is how a
# shop window is read, and ending in the primary key, because a keyset cursor
# needs the ordering to be unique or a page boundary falling between two salons
# of the same name would repeat one of them forever.
SALON_CURSOR_ARITY = 2


def salon_cursor(salon: Salon) -> tuple[str, str]:
    """The sort key of one row, as it travels inside a cursor."""
    return (salon.name, str(salon.id))


class SalonRepository:
    """Access to ``salons``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, salon: Salon) -> Salon:
        """Stage a new salon inside the caller's transaction.

        The flush is what surfaces a constraint violation here, where the
        scenario still knows what it was writing, instead of at the commit.
        """
        self._session.add(salon)
        await self._session.flush()
        return salon

    async def get(self, salon_id: SalonId) -> Salon | None:
        """One salon by identifier, active or not.

        Deliberately unfiltered. An inactive salon disappears from the listing
        but stays reachable by its own identifier, exactly like an archived
        service: a booking made while it was open still names it, and an
        administrator has to be able to reach the salon they just closed.
        """
        return await self._session.get(Salon, salon_id)

    async def page(self, *, request: PageRequest, city: str | None = None) -> Sequence[Salon]:
        """One window of the shop window, ordered by ``(name, id)``.

        **There is no access scope in this query, and that is deliberate.**
        Section 8 of docs/CODING_STANDARDS.md requires every listing to be
        filtered by the role of the caller in the SQL itself, because filtering
        afterwards in Python returns short pages. The catalog listing has no
        such filter to apply: it is the shop window, every salon in it is
        visible to everyone including callers with no token at all, and the
        gateway serves it to anonymous traffic (T6.2). What is filtered instead
        is activity -- a closed salon is not on display.

        Returns one row more than the caller asked for; the scenario turns that
        row into ``next_cursor`` and never returns it.
        """
        statement = select(Salon).where(Salon.is_active.is_(True))
        if city is not None:
            statement = statement.where(Salon.city == city)

        statement = _resume(statement, request)
        result = await self._session.execute(
            statement.order_by(Salon.name, Salon.id).limit(request.window())
        )
        return result.scalars().all()


def _resume(statement: Select[tuple[Salon]], request: PageRequest) -> Select[tuple[Salon]]:
    """Continue after the row the cursor names.

    A row value comparison -- ``(name, id) > (:name, :id)`` -- and not
    ``name > :name AND id > :id``, which is a different predicate and drops
    every row whose name matches the cursor. PostgreSQL implements the first
    directly and can walk the matching index with it.
    """
    key = request.key(arity=SALON_CURSOR_ARITY)
    if key is None:
        return statement

    name, salon_id = key
    # The right hand side is a plain tuple: SQLAlchemy binds each element with
    # the type of the column facing it, which is what makes the uuid bind as a
    # uuid rather than as text.
    return statement.where(tuple_(Salon.name, Salon.id) > (name, cursor_uuid(salon_id)))
