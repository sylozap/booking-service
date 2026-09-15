"""The details ``booking`` reads about one master offering one service.

Returns the contract in :mod:`barber_common.contracts.catalog`. Final price and
duration are resolved through :class:`~barber_catalog.domain.pricing.Offering`.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from barber_catalog.domain.errors import MasterNotFound, MasterServiceNotFound
from barber_catalog.domain.identifiers import MasterId, SalonId, ServiceId
from barber_catalog.domain.pricing import Offering
from barber_catalog.repositories.master_services import MasterServiceRepository
from barber_catalog.repositories.masters import MasterRepository
from barber_catalog.repositories.salons import SalonRepository
from barber_catalog.services.cache import CatalogCache
from barber_common.contracts.catalog import MasterServiceDetails, SalonPolicies
from barber_common.db.session import transaction

__all__ = ["ReadMasterServiceDetails"]


class ReadMasterServiceDetails:
    """Describe one master offering one service, for another service."""

    def __init__(self, session: AsyncSession, cache: CatalogCache) -> None:
        self._session = session
        self._masters = MasterRepository(session)
        self._salons = SalonRepository(session)
        self._links = MasterServiceRepository(session)
        self._cache = cache

    async def execute(
        self,
        *,
        master_id: MasterId,
        service_id: ServiceId,
    ) -> MasterServiceDetails:
        """Read everything at one consistent moment.

        The cache is read first; on a miss the three database reads share one
        transaction, and the result is cached after it closes. A deactivated
        master is reported through ``master_active``; a missing link raises.
        """
        key = await self._cache.offering_key(master_id, service_id)
        cached = await self._cache.read(key, MasterServiceDetails)
        if cached is not None:
            return cached

        async with transaction(self._session):
            master = await self._masters.get(master_id)
            if master is None:
                raise MasterNotFound("No such master")

            link = await self._links.get_active_with_service(
                master_id=master_id, service_id=service_id
            )
            if link is None:
                raise MasterServiceNotFound("This master does not offer this service")

            # A master may only offer services of their own salon, so one salon
            # covers both.
            salon = await self._salons.get(SalonId(master.salon_id))
            if salon is None:  # pragma: no cover - a master cannot outlive its salon
                raise MasterNotFound("No such master")

            offering = Offering(
                base_price=link.service.base_price,
                base_duration_min=link.service.base_duration_min,
                price_override=link.price_override,
                duration_override=link.duration_override,
            )

            details = MasterServiceDetails(
                master_id=master.id,
                salon_id=salon.id,
                master_active=master.is_active,
                service_id=link.service_id,
                service_name=link.service.name,
                duration_min=offering.duration_min,
                price=offering.price,
                currency=link.service.currency,
                salon=SalonPolicies(
                    timezone=salon.timezone,
                    slot_step_min=salon.slot_step_min,
                    booking_min_lead_min=salon.booking_min_lead_min,
                    booking_horizon_days=salon.booking_horizon_days,
                    cancel_deadline_min=salon.cancel_deadline_min,
                ),
            )

        await self._cache.write(key, details)
        return details
