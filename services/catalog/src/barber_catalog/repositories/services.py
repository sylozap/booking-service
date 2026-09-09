"""Services a salon offers."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Select, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from barber_catalog.domain.identifiers import SalonId, ServiceId
from barber_catalog.models.service import Service
from barber_common.pagination import PageRequest, cursor_uuid

__all__ = ["SERVICE_CURSOR_ARITY", "ServiceRepository", "service_cursor"]

# Ordered by ``(name, id)``: a price list is read alphabetically, and the
# primary key at the end keeps the keyset unique when a salon offers two things
# under the same name.
SERVICE_CURSOR_ARITY = 2


def service_cursor(service: Service) -> tuple[str, str]:
    """The sort key of one row, as it travels inside a cursor."""
    return (service.name, str(service.id))


class ServiceRepository:
    """Access to ``services``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, service: Service) -> Service:
        """Stage a new service inside the caller's transaction."""
        self._session.add(service)
        await self._session.flush()
        return service

    async def get(self, service_id: ServiceId) -> Service | None:
        """One service by identifier, archived or not.

        Archived services are deliberately still returned. A booking made a
        year ago names this row, and the history has to be readable; it is the
        listing that hides it (T2.4).
        """
        return await self._session.get(Service, service_id)

    async def page(self, *, salon_id: SalonId, request: PageRequest) -> Sequence[Service]:
        """One window of the price list of a salon, ordered by name.

        Archived services never appear here, and there is no parameter to ask
        for them. Archiving is how a salon withdraws an offering, and a listing
        that could show withdrawn ones would need every caller to remember to
        filter -- which is the mistake the flag exists to prevent. A service
        that has to be inspected after archiving is fetched by identifier.
        """
        statement = select(Service).where(
            Service.salon_id == salon_id,
            Service.is_archived.is_(False),
        )

        statement = _resume(statement, request)
        result = await self._session.execute(
            statement.order_by(Service.name, Service.id).limit(request.window())
        )
        return result.scalars().all()


def _resume(statement: Select[tuple[Service]], request: PageRequest) -> Select[tuple[Service]]:
    """Continue after the row the cursor names, as a row value comparison."""
    key = request.key(arity=SERVICE_CURSOR_ARITY)
    if key is None:
        return statement

    name, service_id = key
    return statement.where(tuple_(Service.name, Service.id) > (name, cursor_uuid(service_id)))
