"""Creating, changing, listing and archiving the services of a salon.

Named ``catalog_services`` rather than ``services``: inside the scenario
package that would read as ``services/services.py``, and the platform already
uses "service" for two different things. The domain word wins in the API and in
the tables; the module name spells out which one is meant
(docs/CODING_STANDARDS.md section 3).

**A service is never deleted.** Bookings snapshot its name and price at the
moment they are made, but they still carry ``service_id``, and a row that
disappears turns every past booking into a dangling identifier. Withdrawing an
offering is archiving it: it leaves the price list and stays reachable by its
own identifier. There is no delete endpoint at all, so the router answers
``405`` -- the absence is the guarantee, not a check that could be forgotten.

**Changing a price does not change what anyone has already been charged.** The
override a master set is a separate column and is untouched here, and a booking
that already exists carries its own snapshot in ``booking``. Editing the price
list is therefore always safe, which is what makes archiving the only operation
that needs care.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from barber_catalog.domain.errors import SalonNotFound, ServiceNotFound
from barber_catalog.domain.identifiers import SalonId, ServiceId
from barber_catalog.models.service import Service
from barber_catalog.repositories.salons import SalonRepository
from barber_catalog.repositories.services import (
    SERVICE_CURSOR_ARITY,
    ServiceRepository,
    service_cursor,
)
from barber_catalog.schemas.services import (
    ServiceCreateRequest,
    ServiceResponse,
    ServiceUpdateRequest,
)
from barber_catalog.services.authorization import require_salon_scope
from barber_common.auth import Principal
from barber_common.db.session import transaction
from barber_common.logging import get_logger
from barber_common.pagination import Page, PageRequest

__all__ = [
    "ArchiveService",
    "CreateService",
    "ListSalonServices",
    "ReadService",
    "UpdateService",
    "service_response",
]

_logger = get_logger(__name__)


def service_response(service: Service) -> ServiceResponse:
    """Map a service row to the response body."""
    return ServiceResponse(
        id=service.id,
        salon_id=service.salon_id,
        name=service.name,
        description=service.description,
        base_duration_min=service.base_duration_min,
        base_price=service.base_price,
        currency=service.currency,
        is_archived=service.is_archived,
        created_at=service.created_at,
        updated_at=service.updated_at,
    )


class CreateService:
    """Add a service to the price list of a salon."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._services = ServiceRepository(session)
        self._salons = SalonRepository(session)

    async def execute(
        self,
        *,
        caller: Principal,
        salon_id: SalonId,
        body: ServiceCreateRequest,
    ) -> ServiceResponse:
        """Create the service.

        No uniqueness on the name: a salon may reasonably offer "Haircut" for
        adults and "Haircut" for children and tell them apart by description,
        and a constraint here would be the service refusing a decision that is
        the salon's to make.
        """
        async with transaction(self._session):
            salon = await self._salons.get(salon_id)
            if salon is None:
                raise SalonNotFound("No such salon")

            require_salon_scope(caller, salon.id)

            service = await self._services.add(
                Service(
                    salon_id=salon.id,
                    name=body.name,
                    description=body.description,
                    base_duration_min=body.base_duration_min,
                    base_price=body.base_price,
                    currency=body.currency,
                )
            )
            response = service_response(service)

        _logger.info("service created", service_id=str(service.id), created_by=caller.subject)
        return response


class UpdateService:
    """Change a service."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._services = ServiceRepository(session)

    async def execute(
        self,
        *,
        caller: Principal,
        service_id: ServiceId,
        body: ServiceUpdateRequest,
    ) -> ServiceResponse:
        """Apply the fields the caller actually sent.

        An archived service can still be edited -- correcting the name of
        something withdrawn last year is exactly the case the archive exists
        for -- and archiving itself is not one of the fields.
        """
        async with transaction(self._session):
            service = await self._services.get(service_id)
            if service is None:
                raise ServiceNotFound("No such service")

            require_salon_scope(caller, service.salon_id)

            for field, value in body.model_dump(exclude_unset=True).items():
                setattr(service, field, value)
            await self._session.flush()
            response = service_response(service)

        _logger.info("service updated", service_id=str(service.id), updated_by=caller.subject)
        return response


class ArchiveService:
    """Withdraw a service from the price list."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._services = ServiceRepository(session)

    async def execute(self, *, caller: Principal, service_id: ServiceId) -> None:
        """Mark the service archived. Repeating this changes nothing.

        Idempotent because the caller cannot always know whether the previous
        attempt landed, and because the requested state -- withdrawn -- is
        exactly the state that already holds. Answering ``409`` on the second
        call would make a retry look like a failure.

        Existing links from masters are left alone. Unarchiving would otherwise
        leave a salon rebuilding every master's price overrides by hand, and
        the archived service is invisible to every read that matters anyway.
        """
        async with transaction(self._session):
            service = await self._services.get(service_id)
            if service is None:
                raise ServiceNotFound("No such service")

            require_salon_scope(caller, service.salon_id)

            if service.is_archived:
                return

            service.is_archived = True
            await self._session.flush()

        _logger.info("service archived", service_id=str(service.id), archived_by=caller.subject)


class ReadService:
    """One service, for anybody who asks."""

    def __init__(self, session: AsyncSession) -> None:
        self._services = ServiceRepository(session)

    async def execute(self, *, service_id: ServiceId) -> ServiceResponse:
        """Read one service, archived or not.

        The archived ones are the point of this endpoint: a client looking at
        a booking from last year has an identifier and needs a name.
        """
        service = await self._services.get(service_id)
        if service is None:
            raise ServiceNotFound("No such service")
        return service_response(service)


class ListSalonServices:
    """The price list of one salon, one page at a time."""

    def __init__(self, session: AsyncSession) -> None:
        self._services = ServiceRepository(session)
        self._salons = SalonRepository(session)

    async def execute(
        self,
        *,
        salon_id: SalonId,
        request: PageRequest,
    ) -> Page[ServiceResponse]:
        """One page of the services a salon currently offers."""
        if await self._salons.get(salon_id) is None:
            raise SalonNotFound("No such salon")

        request.key(arity=SERVICE_CURSOR_ARITY)
        rows: Sequence[Service] = await self._services.page(salon_id=salon_id, request=request)

        page = Page.of(rows, request=request, cursor_of=service_cursor)
        return Page[ServiceResponse](
            items=[service_response(service) for service in page.items],
            next_cursor=page.next_cursor,
        )
