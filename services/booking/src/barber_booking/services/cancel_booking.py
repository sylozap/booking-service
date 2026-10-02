"""Cancelling a booking, by its client or by the salon.

One transaction, and no call to ``catalog``: the deadline travels with the
booking, so a cancellation works when ``catalog`` is down and on the terms the
client booked on. The row is locked first, so a cancellation and a completion
arriving together cannot both decide from the same state.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from barber_booking.domain.booking import Booking
from barber_booking.domain.booking_status import BookingStatus
from barber_booking.domain.identifiers import BookingId
from barber_booking.metrics import count_closed
from barber_booking.repositories.bookings import BookingRepository
from barber_booking.repositories.master_settings import MasterSettingsRepository
from barber_booking.schemas.bookings import BookingCancelRequest, BookingResponse
from barber_booking.services.authorization import booking_actor
from barber_booking.services.booking_response import booking_response
from barber_booking.services.cache import BookingCache
from barber_booking.services.clock import Clock, utc_now
from barber_common.auth import Principal
from barber_common.db.session import transaction
from barber_common.errors import NotFound
from barber_common.events.bookings import (
    BOOKING_AGGREGATE_TYPE,
    BOOKINGS_TOPIC,
    BookingCancelled,
    BookingEventType,
    CancelledBy,
)
from barber_common.logging import get_logger
from barber_common.outbox import OutboxRepository

__all__ = ["CancelBooking"]

_logger = get_logger(__name__)


class CancelBooking:
    """Call one visit off and free its time."""

    def __init__(
        self, session: AsyncSession, cache: BookingCache, *, clock: Clock = utc_now
    ) -> None:
        self._session = session
        self._bookings = BookingRepository(session)
        self._masters = MasterSettingsRepository(session)
        self._outbox = OutboxRepository(session)
        self._cache = cache
        self._clock = clock

    async def execute(
        self,
        *,
        caller: Principal,
        booking_id: BookingId,
        body: BookingCancelRequest | None = None,
    ) -> BookingResponse:
        """Cancel the booking, or answer with it as it is if it already was."""
        now = self._clock()
        reason = body.reason if body is not None else None

        async with transaction(self._session):
            booking = await self._bookings.lock(booking_id)
            if booking is None:
                raise NotFound("Booking not found")

            # The master of a booking may not cancel it, so who the master is
            # does not need to be looked up here.
            cancelled = booking.cancel(
                by=booking_actor(caller, booking, master=None), now=now, reason=reason
            )
            if cancelled is booking:
                return booking_response(booking)

            saved = await self._bookings.save(cancelled)
            count_closed(self._session, [saved])
            # Only for the message the client gets: the time in their salon.
            master = await self._masters.get(saved.master_id)
            await self._outbox.add(
                topic=BOOKINGS_TOPIC,
                aggregate_type=BOOKING_AGGREGATE_TYPE,
                aggregate_id=saved.id,
                event_type=BookingEventType.CANCELLED.value,
                payload=_event_of(saved, timezone=master.timezone if master else None),
            )

        _logger.info("booking cancelled", booking_id=str(saved.id), status=saved.status.value)
        # After the commit: the time is free, and availability cached before
        # the cancellation must not keep hiding it.
        await self._cache.invalidate_master(saved.master_id)
        return booking_response(saved)


def _event_of(booking: Booking, *, timezone: str | None) -> BookingCancelled:
    return BookingCancelled(
        booking_id=booking.id,
        salon_id=booking.salon_id,
        master_id=booking.master_id,
        client_user_id=booking.client_user_id,
        service_name=booking.service.name,
        start_at=booking.start_at,
        end_at=booking.end_at,
        cancelled_by=(
            CancelledBy.CLIENT
            if booking.status is BookingStatus.CANCELLED_BY_CLIENT
            else CancelledBy.SALON
        ),
        reason=booking.cancel_reason,
        timezone=timezone,
    )
