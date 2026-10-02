"""How much of the week ahead is booked, per salon, for the business dashboard.

The load of the masters is a question about the database, not about a request:
nothing happens when a week fills up gradually. So a pass once a minute sums
the open bookings of the next seven days per salon and puts the result in
``bookings_upcoming_seconds``.

Read only, one aggregate, no lock: every replica may run it, and they report
the same numbers. A salon whose bookings are all gone has its series removed
rather than left at the last value, which would show a week still booked.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from barber_booking.domain.identifiers import SalonId
from barber_booking.metrics import BOOKINGS_UPCOMING
from barber_booking.repositories.bookings import BookingRepository
from barber_booking.services.clock import Clock, utc_now
from barber_common.db.session import unit_of_work
from barber_common.logging import get_logger

__all__ = ["BOOKED_TIME_HORIZON", "BookedTimeReporter"]

_logger = get_logger(__name__)

BOOKED_TIME_HORIZON = timedelta(days=7)


class BookedTimeReporter:
    """Keeps ``bookings_upcoming_seconds`` in step with the bookings."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock = utc_now,
        interval_seconds: float = 60.0,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._interval_seconds = interval_seconds
        self._reported: set[SalonId] = set()

    async def run_once(self) -> dict[SalonId, float]:
        """One pass. Returns what it reported."""
        now = self._clock()
        async with unit_of_work(self._session_factory) as session:
            booked = await BookingRepository(session).booked_seconds_by_salon(
                start=now, end=now + BOOKED_TIME_HORIZON
            )

        for salon_id, seconds in booked.items():
            BOOKINGS_UPCOMING.labels(salon=str(salon_id)).set(seconds)
        for salon_id in self._reported - booked.keys():
            BOOKINGS_UPCOMING.remove(str(salon_id))
        self._reported = set(booked)
        return booked

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Report every interval until asked to stop. A failed pass waits for the next."""
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception:
                _logger.exception("booked time pass failed")
            try:
                async with asyncio.timeout(self._interval_seconds):
                    await stop.wait()
            except TimeoutError:
                continue

    @asynccontextmanager
    async def run_in_background(self) -> AsyncIterator[None]:
        """Run the reporter for as long as the block lasts."""
        stop = asyncio.Event()
        task = asyncio.create_task(self.run_forever(stop), name="booked-time")
        try:
            yield
        finally:
            stop.set()
            await task
