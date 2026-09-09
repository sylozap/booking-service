"""Master profiles, and the offerings that hang off one."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Select, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from barber_catalog.domain.identifiers import MasterId, SalonId
from barber_catalog.models.master import Master
from barber_catalog.models.master_service import MasterService
from barber_common.pagination import PageRequest, cursor_uuid

__all__ = ["MASTER_CURSOR_ARITY", "MasterRepository", "master_cursor"]

# Ordered by ``(display_name, id)``: alphabetical inside a salon, ending in the
# primary key so the keyset is unique even when two masters share a name.
MASTER_CURSOR_ARITY = 2


def master_cursor(master: Master) -> tuple[str, str]:
    """The sort key of one row, as it travels inside a cursor."""
    return (master.display_name, str(master.id))


class MasterRepository:
    """Access to ``masters``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, master: Master) -> Master:
        """Stage a new profile inside the caller's transaction."""
        self._session.add(master)
        await self._session.flush()
        return master

    async def get(self, master_id: MasterId) -> Master | None:
        """One profile by identifier, active or not.

        A deactivated master stays readable: their bookings still name them,
        and the internal endpoint of T2.6 reports the state as a flag rather
        than as an error, so that ``booking`` can answer ``master_inactive``
        instead of ``not found``.
        """
        return await self._session.get(Master, master_id)

    async def get_with_offerings(self, master_id: MasterId) -> Master | None:
        """One profile together with the services it offers.

        ``selectinload`` and not a lazy load: the relationship is declared
        ``lazy="raise"``, so a caller that forgets this gets an exception
        rather than an N+1 that only shows up under load. Two statements, one
        round trip each, and no join fan-out over the profile columns.

        Only active links are loaded. Unlinking is a flag (T2.5), and a card
        showing services the master no longer offers would be wrong in exactly
        the way the flag exists to prevent.
        """
        statement = (
            select(Master)
            .where(Master.id == master_id)
            .options(
                selectinload(Master.offerings.and_(MasterService.is_active.is_(True))).selectinload(
                    MasterService.service
                )
            )
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def page(
        self,
        *,
        salon_id: SalonId,
        request: PageRequest,
        is_active: bool | None = None,
    ) -> Sequence[Master]:
        """One window of the masters of a salon, ordered by name.

        Scoped to the salon by the caller's path rather than by their role:
        like the salon listing, this is the shop window and every profile in it
        is visible to everyone (see
        :meth:`~barber_catalog.repositories.salons.SalonRepository.page`).

        ``is_active`` left as ``None`` returns both, which is what an
        administrator reviewing their staff wants; the public card of a salon
        asks for the active ones.
        """
        statement = select(Master).where(Master.salon_id == salon_id)
        if is_active is not None:
            statement = statement.where(Master.is_active.is_(is_active))

        statement = _resume(statement, request)
        result = await self._session.execute(
            statement.order_by(Master.display_name, Master.id).limit(request.window())
        )
        return result.scalars().all()


def _resume(statement: Select[tuple[Master]], request: PageRequest) -> Select[tuple[Master]]:
    """Continue after the row the cursor names, as a row value comparison."""
    key = request.key(arity=MASTER_CURSOR_ARITY)
    if key is None:
        return statement

    display_name, master_id = key
    return statement.where(
        tuple_(Master.display_name, Master.id) > (display_name, cursor_uuid(master_id))
    )
