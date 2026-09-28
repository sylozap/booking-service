"""Reading bookings: a page of them, or one.

Both answer strictly within the caller's scope, and both apply it in the
query. A booking outside the scope does not exist for this caller, so reading
one is ``404`` -- never ``403``, which would confirm that it is there.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from barber_booking.domain.identifiers import BookingId
from barber_booking.domain.visibility import BookingFilters
from barber_booking.repositories.bookings import BookingRepository, booking_cursor
from barber_booking.schemas.bookings import BookingResponse
from barber_booking.services.authorization import booking_scope
from barber_booking.services.booking_response import booking_response
from barber_common.auth import Principal
from barber_common.db.session import transaction
from barber_common.errors import NotFound
from barber_common.pagination import Page, PageRequest

__all__ = ["ListBookings", "ReadBooking"]


class ListBookings:
    """One page of the bookings the caller may see."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._bookings = BookingRepository(session)

    async def execute(
        self, *, caller: Principal, filters: BookingFilters, request: PageRequest
    ) -> Page[BookingResponse]:
        async with transaction(self._session):
            rows = await self._bookings.page(
                scope=booking_scope(caller), filters=filters, request=request
            )
        page = Page.of(rows, request=request, cursor_of=booking_cursor)
        return Page[BookingResponse](
            items=[booking_response(booking) for booking in page.items],
            next_cursor=page.next_cursor,
        )


class ReadBooking:
    """One booking, if the caller may see it."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._bookings = BookingRepository(session)

    async def execute(self, *, caller: Principal, booking_id: BookingId) -> BookingResponse:
        async with transaction(self._session):
            booking = await self._bookings.get_visible(booking_id, booking_scope(caller))
        if booking is None:
            raise NotFound("Booking not found")
        return booking_response(booking)
