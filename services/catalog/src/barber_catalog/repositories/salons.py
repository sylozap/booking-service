"""Salons: the shop window listing and the single row behind it."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Select, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from barber_catalog.domain.identifiers import SalonId
from barber_catalog.models.salon import Salon
from barber_common.pagination import PageRequest, cursor_uuid

__all__ = ["SALON_CURSOR_ARITY", "SalonRepository", "salon_cursor"]

# The listing is ordered by ``(name, id)``: alphabetical, and unique for the
# keyset cursor.
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
        """One salon by identifier, active or not."""
        return await self._session.get(Salon, salon_id)

    async def page(self, *, request: PageRequest, city: str | None = None) -> Sequence[Salon]:
        """One window of the active salons, ordered by ``(name, id)``.

        Visible to everyone, so there is no access scope. Returns one row more
        than requested; the scenario turns it into ``next_cursor``.
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

    Uses the row value comparison ``(name, id) > (:name, :id)``.
    """
    key = request.key(arity=SALON_CURSOR_ARITY)
    if key is None:
        return statement

    name, salon_id = key
    # The right hand side is a plain tuple: SQLAlchemy binds each element with
    # the type of the column facing it, which is what makes the uuid bind as a
    # uuid rather than as text.
    return statement.where(tuple_(Salon.name, Salon.id) > (name, cursor_uuid(salon_id)))
