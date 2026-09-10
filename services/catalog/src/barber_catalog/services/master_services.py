"""Which services a master offers, and on what terms.

``PUT`` states the whole terms of one link and ``DELETE`` withdraws it. Both
are idempotent, which is the reason ``PUT`` was chosen over ``POST``: an
administrator setting a price list for a dozen masters retries, and a create
that fails the second time would make them check each one by hand.

**A master may only offer services of their own salon.** The check is here
rather than in the database: a foreign key cannot express "the salon of the
master equals the salon of the service", and a composite key that could would
mean carrying ``salon_id`` in the link table just to constrain it. The answer
is ``422`` -- both entities exist and the caller may see both, so what is wrong
is the combination.

**Withdrawing is a flag, not a delete.** The row carries the overrides, and
dropping it would mean a master who stops offering something for a month has to
have their prices entered again. It also makes ``DELETE`` safe to repeat, which
a delete of an absent row is not.

**Both publish ``master.updated``, keyed by the master.** The six events of
T2.9 are named after the master and the service aggregates, and the link is
neither -- but what a master offers is, from the outside, part of that master,
and it is exactly what the internal endpoint of T2.6 answers with. Without an
event here, a consumer caching that answer (T3.6) would keep serving a price
that has been overridden since. Keying it by the master is what keeps it in
order with the other events about the same profile.
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
from barber_catalog.repositories.services import ServiceRepository
from barber_catalog.schemas.master_services import MasterServiceRequest, MasterServiceResponse
from barber_catalog.services.authorization import require_salon_scope
from barber_catalog.services.cache import CatalogCache
from barber_catalog.services.masters import master_snapshot
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

        ``PUT`` states the whole state of the link, so an override left out of
        the body is an override that is gone -- which is how one is removed.
        Assigning both fields unconditionally rather than only the ones present
        is what makes that true.

        Order of the checks: the master decides the salon, the salon decides
        whether the caller may be here at all, and only then does the service
        get looked at. Checking the service first would let a caller with no
        rights anywhere find out which service identifiers are real.
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
                payload=master_snapshot(master),
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

        **Existing bookings are untouched.** A booking holds its own snapshot
        of the service name, price and duration, taken when it was made
        (docs/05-data-model.md), so what a master offers today has no bearing
        on what was already agreed.

        A link that is not there, or is already withdrawn, still answers
        ``204``: the requested state is the state that holds. A missing
        *master* is still ``404`` -- that is a wrong request rather than a
        repeated one.
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
                payload=master_snapshot(master),
            )
            await self._session.flush()

        await self._cache.invalidate()

        _logger.info(
            "master service unlinked",
            master_id=str(master_id),
            service_id=str(service_id),
            unlinked_by=caller.subject,
        )
