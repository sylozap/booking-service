"""Moving a booking to another time, in one transaction.

Not a cancellation followed by a new booking. The same row is updated, so the
booking keeps its identity and its history, and if the new time is lost to
somebody else the transaction rolls back whole: the client still holds the old
time, as if they had never asked.

The order is the one of creating a booking. The booking is read and the move
checked first, so a request that may not be made never reaches ``catalog``.
``catalog`` is asked outside any transaction. Everything after it -- the lock,
the schedule, the update, the event -- is one transaction, and the exclusion
constraint judges the new time at the moment of the update.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from barber_booking.clients.catalog import CatalogClient
from barber_booking.domain.booking import Booking, reminder_for
from barber_booking.domain.errors import MasterInactive
from barber_booking.domain.identifiers import BookingId
from barber_booking.domain.policies import (
    validate_horizon,
    validate_lead_time,
    validate_start_is_bookable,
)
from barber_booking.domain.schedule import working_intervals
from barber_booking.repositories.bookings import BookingRepository, is_overlap
from barber_booking.repositories.master_settings import MasterSettingsRepository
from barber_booking.repositories.schedule import ScheduleRepository
from barber_booking.schemas.bookings import BookingRescheduleRequest, BookingResponse
from barber_booking.services.alternatives import SlotTaken
from barber_booking.services.authorization import booking_actor
from barber_booking.services.booking_response import booking_response
from barber_booking.services.cache import BookingCache
from barber_booking.services.clock import Clock, utc_now
from barber_booking.services.create_booking import DEFAULT_REMINDER_LEAD
from barber_booking.services.offerings import ReadOffering
from barber_common.auth import Principal
from barber_common.contracts.catalog import MasterServiceDetails
from barber_common.db.session import transaction
from barber_common.errors import NotFound
from barber_common.events.bookings import (
    BOOKING_AGGREGATE_TYPE,
    BOOKINGS_TOPIC,
    BookingEventType,
    BookingRescheduled,
)
from barber_common.logging import get_logger
from barber_common.outbox import OutboxRepository

__all__ = ["RescheduleBooking"]

_logger = get_logger(__name__)


class RescheduleBooking:
    """Move one booking to another start."""

    def __init__(
        self,
        session: AsyncSession,
        cache: BookingCache,
        catalog: CatalogClient,
        *,
        clock: Clock = utc_now,
        reminder_lead: timedelta = DEFAULT_REMINDER_LEAD,
    ) -> None:
        self._session = session
        self._bookings = BookingRepository(session)
        self._masters = MasterSettingsRepository(session)
        self._schedule = ScheduleRepository(session)
        self._outbox = OutboxRepository(session)
        self._offerings = ReadOffering(catalog, cache)
        self._slot_taken = SlotTaken(session)
        self._cache = cache
        self._clock = clock
        self._reminder_lead = reminder_lead

    async def execute(
        self, *, caller: Principal, booking_id: BookingId, body: BookingRescheduleRequest
    ) -> BookingResponse:
        """Move the booking, or answer with it as it is if it is already there."""
        now = self._clock()

        async with transaction(self._session):
            current = await self._bookings.get(booking_id)
        if current is None:
            raise NotFound("Booking not found")
        if not current.check_move(
            by=booking_actor(caller, current, master=None), now=now, to=body.start_at
        ):
            return booking_response(current)

        offering = await self._offerings.execute(
            master_id=current.master_id, service_id=current.service.service_id
        )
        if not offering.master_active:
            raise MasterInactive("This master takes no bookings")
        zone = ZoneInfo(offering.salon.timezone)
        validate_lead_time(body.start_at, now=now, lead_min=offering.salon.booking_min_lead_min)
        validate_horizon(
            body.start_at, now=now, horizon_days=offering.salon.booking_horizon_days, zone=zone
        )

        try:
            previous, moved = await self._write(
                caller=caller, booking_id=booking_id, body=body, offering=offering, now=now
            )
        except IntegrityError as error:
            if not is_overlap(error):
                raise
            raise await self._slot_taken.execute(
                master_id=current.master_id,
                wanted=body.start_at,
                duration_min=current.duration_min,
                offering=offering,
                now=now,
                ignoring=current.id,
            ) from error

        if moved is not previous:
            _logger.info("booking rescheduled", booking_id=str(moved.id))
            # After the commit: the old time is free and the new one is not.
            await self._cache.invalidate_master(moved.master_id)
        return booking_response(moved)

    async def _write(
        self,
        *,
        caller: Principal,
        booking_id: BookingId,
        body: BookingRescheduleRequest,
        offering: MasterServiceDetails,
        now: datetime,
    ) -> tuple[Booking, Booking]:
        """The transaction: the row at its new time and the event, or neither."""
        async with transaction(self._session):
            # Locked, and every rule checked again: the booking may have changed
            # while catalog was being asked.
            booking = await self._bookings.lock(booking_id)
            if booking is None:
                raise NotFound("Booking not found")

            settings = await self._masters.get(booking.master_id)
            buffer_min = settings.buffer_after_min if settings is not None else 0
            moved = booking.reschedule(
                by=booking_actor(caller, booking, master=None),
                now=now,
                start_at=body.start_at,
                buffer_min=buffer_min,
                reminder_at=reminder_for(body.start_at, now=now, lead=self._reminder_lead),
            )
            if moved is booking:
                return booking, booking

            zone = ZoneInfo(offering.salon.timezone)
            day = body.start_at.astimezone(zone).date()
            validate_start_is_bookable(
                body.start_at,
                work=working_intervals(
                    day,
                    await self._schedule.templates_from(booking.master_id, day),
                    await self._schedule.exceptions_on(booking.master_id, day),
                    zone,
                ),
                # The snapshot's duration, not today's: the client keeps the
                # service they bought.
                duration_min=booking.duration_min,
                buffer_min=buffer_min,
                step_min=offering.salon.slot_step_min,
            )

            saved = await self._bookings.save(moved)
            await self._outbox.add(
                topic=BOOKINGS_TOPIC,
                aggregate_type=BOOKING_AGGREGATE_TYPE,
                aggregate_id=saved.id,
                event_type=BookingEventType.RESCHEDULED.value,
                payload=_event_of(booking, saved),
            )
        return booking, saved


def _event_of(previous: Booking, moved: Booking) -> BookingRescheduled:
    return BookingRescheduled(
        booking_id=moved.id,
        salon_id=moved.salon_id,
        master_id=moved.master_id,
        client_user_id=moved.client_user_id,
        service_name=moved.service.name,
        previous_start_at=previous.start_at,
        previous_end_at=previous.end_at,
        start_at=moved.start_at,
        end_at=moved.end_at,
        reminder_at=moved.reminder_at,
    )
