"""Which services a master offers, and on what terms.

``PUT`` sets the whole terms of one link and ``DELETE`` withdraws it; both are
idempotent. A master may only offer services of their own salon, otherwise the
answer is ``422``. Withdrawing sets a flag and keeps the overrides. Both
operations publish ``master.updated``, keyed by the master.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from barber_catalog.domain.errors import (
    MasterNotFound,
    ServiceArchived,
    ServiceFromAnotherSalon,
    ServiceNotFound,
)
from barber_catalog.domain.identifiers import MasterId, ServiceId
from barber_catalog.domain.pricing import Offering
from barber_catalog.models.master_service import MasterService
from barber_catalog.models.service import Service
from barber_catalog.repositories.master_services import MasterServiceRepository
from barber_catalog.repositories.masters import MasterRepository
from barber_catalog.repositories.salons import SalonRepository
from barber_catalog.repositories.services import ServiceRepository
from barber_catalog.schemas.master_services import MasterServiceRequest, MasterServiceResponse
from barber_catalog.services.authorization import require_salon_scope
from barber_catalog.services.cache import CatalogCache
from barber_catalog.services.masters import snapshot_of
from barber_common.auth import Principal
from barber_common.db.session import transaction
from barber_common.events.catalog import (
    CATALOG_MASTERS_TOPIC,
    MASTER_AGGREGATE_TYPE,
    MasterEventType,
)
from barber_common.logging import get_logger
from barber_common.outbox import OutboxRepository

__all__ = ["LinkMasterService", "UnlinkMasterService", "master_service_response"]

_logger = get_logger(__name__)


def master_service_response(link: MasterService, service: Service) -> MasterServiceResponse:
    """Map a link to the response body, resolving the final figures.

    ``service`` is passed rather than read off ``link.service``: the scenario
    already holds it, and the relationship raises rather than lazily loading.
    """
    offering = Offering(
        base_price=service.base_price,
        base_duration_min=service.base_duration_min,
        price_override=link.price_override,
        duration_override=link.duration_override,
    )
    return MasterServiceResponse(
        master_id=link.master_id,
        service_id=link.service_id,
        price=offering.price,
        duration_min=offering.duration_min,
        currency=service.currency,
        price_override=link.price_override,
        duration_override=link.duration_override,
        base_price=service.base_price,
        base_duration_min=service.base_duration_min,
        created_at=link.created_at,
        updated_at=link.updated_at,
    )


class LinkMasterService:
    """Say that a master offers a service, and on what terms."""

    def __init__(self, session: AsyncSession, cache: CatalogCache) -> None:
        self._session = session
        self._masters = MasterRepository(session)
        self._salons = SalonRepository(session)
        self._services = ServiceRepository(session)
        self._links = MasterServiceRepository(session)
        self._outbox = OutboxRepository(session)
        self._cache = cache

    async def execute(
        self,
        *,
        caller: Principal,
        master_id: MasterId,
        service_id: ServiceId,
        body: MasterServiceRequest,
    ) -> MasterServiceResponse:
        """Create or replace the link.

        An override left out of the body is removed. The caller's access to the
        salon is checked before the service is looked up.
        """
        async with transaction(self._session):
            master = await self._masters.get(master_id)
            if master is None:
                raise MasterNotFound("No such master")

            require_salon_scope(caller, master.salon_id)

            service = await self._services.get(service_id)
            if service is None:
                raise ServiceNotFound("No such service")
            if service.salon_id != master.salon_id:
                raise ServiceFromAnotherSalon(
                    "This service belongs to a different salon than the master"
                )
            if service.is_archived:
                raise ServiceArchived("This service has been withdrawn by the salon")

            link = await self._links.get(master_id=master_id, service_id=service_id)
            if link is None:
                link = await self._links.add(
                    MasterService(master_id=master_id, service_id=service_id)
                )

            link.price_override = body.price_override
            link.duration_override = body.duration_override
            # A link that was withdrawn and is being stated again is active
            # again. The alternative -- refusing until it is deleted for real
            # -- would make PUT stop being a statement of the desired state.
            link.is_active = True
            await self._session.flush()

            await self._outbox.add(
                topic=CATALOG_MASTERS_TOPIC,
                aggregate_type=MASTER_AGGREGATE_TYPE,
                aggregate_id=master.id,
                event_type=MasterEventType.UPDATED.value,
                payload=await snapshot_of(master, self._salons),
            )

            response = master_service_response(link, service)

        await self._cache.invalidate()

        _logger.info(
            "master service linked",
            master_id=str(master_id),
            service_id=str(service_id),
            linked_by=caller.subject,
        )
        return response


class UnlinkMasterService:
    """Say that a master no longer offers a service."""

    def __init__(self, session: AsyncSession, cache: CatalogCache) -> None:
        self._session = session
        self._masters = MasterRepository(session)
        self._salons = SalonRepository(session)
        self._links = MasterServiceRepository(session)
        self._outbox = OutboxRepository(session)
        self._cache = cache

    async def execute(
        self,
        *,
        caller: Principal,
        master_id: MasterId,
        service_id: ServiceId,
    ) -> None:
        """Withdraw the link, keeping the terms it carried.

        Existing bookings are not affected. A missing or already withdrawn link
        still answers ``204``; a missing master answers ``404``.
        """
        async with transaction(self._session):
            master = await self._masters.get(master_id)
            if master is None:
                raise MasterNotFound("No such master")

            require_salon_scope(caller, master.salon_id)

            link = await self._links.get(master_id=master_id, service_id=service_id)
            if link is None or not link.is_active:
                return

            link.is_active = False
            await self._outbox.add(
                topic=CATALOG_MASTERS_TOPIC,
                aggregate_type=MASTER_AGGREGATE_TYPE,
                aggregate_id=master.id,
                event_type=MasterEventType.UPDATED.value,
                payload=await snapshot_of(master, self._salons),
            )
            await self._session.flush()

        await self._cache.invalidate()

        _logger.info(
            "master service unlinked",
            master_id=str(master_id),
            service_id=str(service_id),
            unlinked_by=caller.subject,
        )
