"""Recording how a visit went: completed, or the client did not come.

The time stays taken either way -- both statuses are in the exclusion
constraint -- so availability has nothing to forget and the cache is left
alone.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from barber_booking.domain.booking import Booking
from barber_booking.domain.booking_status import BookingStatus
from barber_booking.domain.identifiers import BookingId
from barber_booking.repositories.bookings import BookingRepository
from barber_booking.repositories.master_settings import MasterSettingsRepository
from barber_booking.schemas.bookings import BookingResponse
from barber_booking.services.authorization import booking_actor
from barber_booking.services.booking_response import booking_response
from barber_booking.services.clock import Clock, utc_now
from barber_common.auth import Principal
from barber_common.db.session import transaction
from barber_common.errors import NotFound
from barber_common.events.bookings import (
    BOOKING_AGGREGATE_TYPE,
    BOOKINGS_TOPIC,
    BookingCompleted,
    BookingEventType,
    BookingNoShow,
)
from barber_common.logging import get_logger
from barber_common.outbox import OutboxRepository

__all__ = ["CloseVisit"]

_logger = get_logger(__name__)


class CloseVisit:
    """Mark one visit that has started as completed or as a no-show."""

    def __init__(self, session: AsyncSession, *, clock: Clock = utc_now) -> None:
        self._session = session
        self._bookings = BookingRepository(session)
        self._masters = MasterSettingsRepository(session)
        self._outbox = OutboxRepository(session)
        self._clock = clock

    async def execute(
        self, *, caller: Principal, booking_id: BookingId, outcome: BookingStatus
    ) -> BookingResponse:
        """Record the outcome, or answer with the booking if it is already recorded."""
        now = self._clock()

        async with transaction(self._session):
            booking = await self._bookings.lock(booking_id)
            if booking is None:
                raise NotFound("Booking not found")

            # The master is recognised by the account behind their settings.
            master = await self._masters.get(booking.master_id)
            closed = booking.close(
                outcome=outcome, by=booking_actor(caller, booking, master), now=now
            )
            if closed is booking:
                return booking_response(booking)

            saved = await self._bookings.save(closed)
            event_type, payload = _event_of(saved, timezone=master.timezone if master else None)
            await self._outbox.add(
                topic=BOOKINGS_TOPIC,
                aggregate_type=BOOKING_AGGREGATE_TYPE,
                aggregate_id=saved.id,
                event_type=event_type.value,
                payload=payload,
            )

        _logger.info("visit closed", booking_id=str(saved.id), status=saved.status.value)
        return booking_response(saved)


def _event_of(
    booking: Booking, *, timezone: str | None
) -> tuple[BookingEventType, BookingCompleted | BookingNoShow]:
    completed = booking.status is BookingStatus.COMPLETED
    payload_type = BookingCompleted if completed else BookingNoShow
    payload = payload_type(
        booking_id=booking.id,
        salon_id=booking.salon_id,
        master_id=booking.master_id,
        client_user_id=booking.client_user_id,
        service_name=booking.service.name,
        start_at=booking.start_at,
        end_at=booking.end_at,
        timezone=timezone,
    )
    return (BookingEventType.COMPLETED if completed else BookingEventType.NO_SHOW), payload
