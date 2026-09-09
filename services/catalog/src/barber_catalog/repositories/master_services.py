"""The link between a master and a service of their salon."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from barber_catalog.domain.identifiers import MasterId, ServiceId
from barber_catalog.models.master_service import MasterService

__all__ = ["MasterServiceRepository"]


class MasterServiceRepository:
    """Access to ``master_services``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, *, master_id: MasterId, service_id: ServiceId) -> MasterService | None:
        """One link, active or not.

        Unfiltered on purpose: the scenario that reactivates a link has to find
        the inactive row in order to reuse the overrides stored on it, and only
        the readers filter.
        """
        return await self._session.get(MasterService, (master_id, service_id))

    async def get_active_with_service(
        self,
        *,
        master_id: MasterId,
        service_id: ServiceId,
    ) -> MasterService | None:
        """One active link together with the service behind it.

        What the internal endpoint of T2.6 reads. ``selectinload`` because the
        relationship raises rather than lazily loading, and the base price and
        duration on the other side are exactly what is being asked for.
        """
        statement = (
            select(MasterService)
            .where(
                MasterService.master_id == master_id,
                MasterService.service_id == service_id,
                MasterService.is_active.is_(True),
            )
            .options(selectinload(MasterService.service))
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def add(self, link: MasterService) -> MasterService:
        """Stage a new link inside the caller's transaction."""
        self._session.add(link)
        await self._session.flush()
        return link
