"""Creating, changing and listing salons.

The salon is the root of everything else in the catalog: a master belongs to
one, a service belongs to one, and the four booking policies live on it. That
is why creating one is reserved for ``super_admin`` -- a salon administrator
who could create salons could create the salon they administer -- while
changing one is open to the administrator of that salon.

Reading is open to everybody, including callers with no token: the catalog is
the shop window, and the gateway serves it to anonymous traffic (T6.2).
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from barber_catalog.domain.errors import SalonNotFound
from barber_catalog.domain.identifiers import SalonId
from barber_catalog.models.salon import Salon
from barber_catalog.repositories.salons import (
    SALON_CURSOR_ARITY,
    SalonRepository,
    salon_cursor,
)
from barber_catalog.schemas.salons import (
    SalonCreateRequest,
    SalonResponse,
    SalonUpdateRequest,
)
from barber_catalog.services.authorization import require_salon_scope
from barber_common.auth import Principal
from barber_common.db.session import transaction
from barber_common.logging import get_logger
from barber_common.pagination import Page, PageRequest

__all__ = ["CreateSalon", "ListSalons", "ReadSalon", "UpdateSalon", "salon_response"]

_logger = get_logger(__name__)


def salon_response(salon: Salon) -> SalonResponse:
    """Map a row to the response body.

    Explicit and in one place: the catalog repository hands back ORM objects
    (docs/CODING_STANDARDS.md section 5), and a router returning one directly
    would publish the database schema as the API contract.
    """
    return SalonResponse(
        id=salon.id,
        name=salon.name,
        description=salon.description,
        address=salon.address,
        city=salon.city,
        phone=salon.phone,
        timezone=salon.timezone,
        slot_step_min=salon.slot_step_min,
        booking_min_lead_min=salon.booking_min_lead_min,
        booking_horizon_days=salon.booking_horizon_days,
        cancel_deadline_min=salon.cancel_deadline_min,
        is_active=salon.is_active,
        created_at=salon.created_at,
        updated_at=salon.updated_at,
    )


class CreateSalon:
    """Open a new salon."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._salons = SalonRepository(session)

    async def execute(self, *, caller: Principal, body: SalonCreateRequest) -> SalonResponse:
        """Create the salon and hand back what now exists.

        No scope check beyond the role: ``super_admin`` is global, and there is
        no salon to be inside of yet. The endpoint admits nobody else.
        """
        async with transaction(self._session):
            salon = await self._salons.add(
                Salon(
                    name=body.name,
                    description=body.description,
                    address=body.address,
                    city=body.city,
                    phone=body.phone,
                    timezone=body.timezone,
                    slot_step_min=body.slot_step_min,
                    booking_min_lead_min=body.booking_min_lead_min,
                    booking_horizon_days=body.booking_horizon_days,
                    cancel_deadline_min=body.cancel_deadline_min,
                )
            )
            # Mapped while the transaction is still open: the row is read in
            # the transaction that produced it, so the response cannot describe
            # a state the commit never reached.
            response = salon_response(salon)

        _logger.info("salon created", salon_id=str(salon.id), created_by=caller.subject)
        return response


class UpdateSalon:
    """Change a salon, including its booking policies."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._salons = SalonRepository(session)

    async def execute(
        self,
        *,
        caller: Principal,
        salon_id: SalonId,
        body: SalonUpdateRequest,
    ) -> SalonResponse:
        """Apply the fields the caller actually sent.

        ``exclude_unset`` is what separates "clear the description" from "leave
        the description alone": both arrive as ``None`` on the parsed body, and
        only the set of keys present in the request tells them apart.

        The salon is loaded before the scope is checked, because the scope is a
        question about this salon and the answer is in the row. A caller who
        may not touch it gets ``403`` whether or not it exists -- ordering it
        the other way would turn the endpoint into a way of discovering which
        salon identifiers are real.
        """
        async with transaction(self._session):
            salon = await self._salons.get(salon_id)
            if salon is None:
                raise SalonNotFound("No such salon")

            require_salon_scope(caller, salon.id)

            for field, value in body.model_dump(exclude_unset=True).items():
                setattr(salon, field, value)
            await self._session.flush()
            # Inside the transaction, for the same reason as in CreateSalon.
            response = salon_response(salon)

        _logger.info("salon updated", salon_id=str(salon.id), updated_by=caller.subject)
        return response


class ReadSalon:
    """One salon, for anybody who asks."""

    def __init__(self, session: AsyncSession) -> None:
        self._salons = SalonRepository(session)

    async def execute(self, *, salon_id: SalonId) -> SalonResponse:
        """Read one salon, active or not.

        An inactive salon stays readable by identifier: a booking made while it
        was open still names it, and the administrator who closed it has to be
        able to reach it. It is the listing that hides it.
        """
        salon = await self._salons.get(salon_id)
        if salon is None:
            raise SalonNotFound("No such salon")
        return salon_response(salon)


class ListSalons:
    """The shop window, one page at a time."""

    def __init__(self, session: AsyncSession) -> None:
        self._salons = SalonRepository(session)

    async def execute(
        self,
        *,
        request: PageRequest,
        city: str | None = None,
    ) -> Page[SalonResponse]:
        """One page of active salons, ordered by name.

        The cursor is validated here, before the query: a malformed one is a
        ``422`` about the request rather than a failure at the point the
        statement binds its parameters.
        """
        request.key(arity=SALON_CURSOR_ARITY)
        rows: Sequence[Salon] = await self._salons.page(request=request, city=city)

        # The cursor is built from the row, not from the response body: the
        # sort key is a property of the ordering, and a response schema that
        # later stops carrying ``name`` would silently break paging.
        page = Page.of(rows, request=request, cursor_of=salon_cursor)
        return Page[SalonResponse](
            items=[salon_response(salon) for salon in page.items],
            next_cursor=page.next_cursor,
        )
