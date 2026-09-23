"""Access to ``bookings``."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from barber_booking.models.booking import Booking

__all__ = ["BookingRepository"]


class BookingRepository:
    """Writing and reading bookings, inside the caller's transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, booking: Booking) -> Booking:
        """Stage a booking and let the database judge it.

        The flush is what makes the exclusion constraint speak: without it the
        conflict would surface at commit, outside the scenario that can answer
        it.
        """
        self._session.add(booking)
        await self._session.flush()
        return booking
