"""Who a master is, for another service.

Not cached: ``booking`` asks only for a master whose ``master.created`` it
never received, once, and keeps the answer.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from barber_catalog.domain.errors import MasterNotFound
from barber_catalog.domain.identifiers import MasterId, SalonId
from barber_catalog.repositories.masters import MasterRepository
from barber_catalog.repositories.salons import SalonRepository
from barber_common.contracts.catalog import MasterProfile
from barber_common.db.session import transaction

__all__ = ["ReadMasterProfile"]


class ReadMasterProfile:
    """Describe one master: account, salon, zone and whether they work."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._masters = MasterRepository(session)
        self._salons = SalonRepository(session)

    async def execute(self, *, master_id: MasterId) -> MasterProfile:
        """A deactivated master is described too: ``is_active`` says so."""
        async with transaction(self._session):
            master = await self._masters.get(master_id)
            if master is None:
                raise MasterNotFound("No such master")
            salon = await self._salons.get(SalonId(master.salon_id))
            if salon is None:  # pragma: no cover - a master cannot outlive its salon
                raise MasterNotFound("No such master")

            return MasterProfile(
                master_id=master.id,
                salon_id=salon.id,
                user_id=master.user_id,
                is_active=master.is_active,
                timezone=salon.timezone,
            )
