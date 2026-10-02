"""A master leaves, and the visits booked with them are called off.

The one place where an event of another service changes state here. It runs in
the transaction the consumer runner opens, together with the row that marks
the event processed: either every booking is cancelled, its event queued and
the event recorded as handled, or none of it happened and the event comes
again.

The size of that transaction is bounded by the booking horizon of the salon --
a master's future is at most a couple of months of visits -- and every step is
one statement whatever the number of bookings.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from barber_booking.domain.booking import MASTER_DEACTIVATED, Actor, Booking
from barber_booking.domain.identifiers import MasterId
from barber_booking.metrics import count_closed
from barber_booking.repositories.bookings import BookingRepository
from barber_booking.repositories.master_settings import MasterSettingsRepository
from barber_booking.services.clock import Clock, utc_now
from barber_common.db.session import transaction
from barber_common.events.bookings import (
    BOOKING_AGGREGATE_TYPE,
    BOOKINGS_TOPIC,
    BookingCancelled,
    BookingEventType,
    CancelledBy,
)
from barber_common.logging import get_logger
from barber_common.outbox import OutboxRepository

__all__ = ["CancelMasterBookings"]

_logger = get_logger(__name__)


class CancelMasterBookings:
    """Mark a master inactive and cancel every visit of theirs still ahead."""

    def __init__(self, session: AsyncSession, *, clock: Clock = utc_now) -> None:
        self._session = session
        self._bookings = BookingRepository(session)
        self._masters = MasterSettingsRepository(session)
        self._outbox = OutboxRepository(session)
        self._clock = clock

    async def execute(self, *, master_id: MasterId, cause: UUID) -> int:
        """Cancel as the salon, with nobody named. Returns how many were cancelled.

        ``cause`` is the ``event_id`` of ``master.deactivated``: each
        ``booking.cancelled`` names it, so a client's notification can be traced
        back to the deactivation that caused it.
        """
        now = self._clock()
        async with transaction(self._session):
            # A master booking never had settings for has nothing to switch
            # off; their bookings are cancelled all the same.
            master = await self._masters.follow(master_id, timezone=None, is_active=False)
            timezone = master.timezone if master is not None else None

            upcoming = await self._bookings.lock_upcoming(master_id, now=now)
            cancelled = [_cancelled(booking, now) for booking in upcoming]
            await self._bookings.save_all(cancelled)
            # Counted when the runner commits, not here: a retried handler
            # would otherwise count the same cancellations again.
            count_closed(self._session, cancelled)
            await self._outbox.add_batch(
                topic=BOOKINGS_TOPIC,
                aggregate_type=BOOKING_AGGREGATE_TYPE,
                event_type=BookingEventType.CANCELLED.value,
                events=[
                    (booking.id, _event_of(booking, timezone=timezone)) for booking in cancelled
                ],
                causation_id=cause,
            )

        _logger.info(
            "bookings of a deactivated master cancelled",
            master_id=str(master_id),
            cancelled=len(cancelled),
        )
        return len(cancelled)


def _cancelled(booking: Booking, now: datetime) -> Booking:
    return booking.cancel(by=Actor.the_salon(), now=now, reason=MASTER_DEACTIVATED)


def _event_of(booking: Booking, *, timezone: str | None) -> BookingCancelled:
    return BookingCancelled(
        booking_id=booking.id,
        salon_id=booking.salon_id,
        master_id=booking.master_id,
        client_user_id=booking.client_user_id,
        service_name=booking.service.name,
        start_at=booking.start_at,
        end_at=booking.end_at,
        cancelled_by=CancelledBy.SALON,
        reason=booking.cancel_reason,
        timezone=timezone,
    )
