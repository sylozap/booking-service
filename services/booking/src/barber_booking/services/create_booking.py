"""Creating a booking: the one write that holds the invariant of the platform.

The order of the steps is the whole design. ``catalog`` is asked **before** any
transaction opens, because holding a pooled connection across a network call is
how a pool runs out. Everything after it -- the idempotency key, the row, the
event -- is one transaction, so a booking that exists always has its event on
the way and its key stored beside it.

Whether the time is free is not checked anywhere here. The exclusion constraint
answers that at the moment of the insert, and a violation becomes
``409 slot_taken``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from barber_booking.clients.catalog import CatalogClient
from barber_booking.domain.booking import Actor, Booking, ServiceSnapshot, reminder_for
from barber_booking.domain.errors import MasterInactive, NotAllowedForActor
from barber_booking.domain.identifiers import BookingId, MasterId, SalonId, ServiceId, UserId
from barber_booking.domain.policies import (
    validate_horizon,
    validate_lead_time,
    validate_start_is_bookable,
)
from barber_booking.domain.schedule import working_intervals
from barber_booking.domain.time_range import TimeRange
from barber_booking.metrics import BOOKINGS_CREATED
from barber_booking.repositories.bookings import BookingRepository, is_overlap
from barber_booking.repositories.master_settings import MasterSettingsRepository
from barber_booking.repositories.schedule import ScheduleRepository
from barber_booking.schemas.bookings import BookingCreateRequest, BookingResponse
from barber_booking.services.alternatives import SlotTaken
from barber_booking.services.authorization import booking_creator, may_book_for_others
from barber_booking.services.booking_response import booking_response
from barber_booking.services.cache import BookingCache
from barber_booking.services.clock import Clock, utc_now
from barber_booking.services.offerings import ReadOffering
from barber_common.auth import Principal
from barber_common.contracts.catalog import MasterServiceDetails
from barber_common.db.errors import is_unique_violation
from barber_common.db.session import transaction
from barber_common.events.bookings import (
    BOOKING_AGGREGATE_TYPE,
    BOOKINGS_TOPIC,
    BookingCreated,
    BookingEventType,
)
from barber_common.idempotency import (
    DEFAULT_TTL,
    IdempotencyRepository,
    IdempotentRequest,
    StoredResponse,
)
from barber_common.outbox import OutboxRepository

__all__ = ["CREATED_STATUS", "CreateBooking"]

CREATED_STATUS = 201

# The primary key a second request holding the same idempotency key loses on.
IDEMPOTENCY_CONSTRAINT = "pk_idempotency_keys"

DEFAULT_REMINDER_LEAD = timedelta(hours=4)


class CreateBooking:
    """Book one master for one service at one moment."""

    def __init__(
        self,
        session: AsyncSession,
        cache: BookingCache,
        catalog: CatalogClient,
        *,
        clock: Clock = utc_now,
        idempotency_ttl: timedelta = DEFAULT_TTL,
        reminder_lead: timedelta = DEFAULT_REMINDER_LEAD,
    ) -> None:
        self._session = session
        self._masters = MasterSettingsRepository(session)
        self._schedule = ScheduleRepository(session)
        self._bookings = BookingRepository(session)
        self._slot_taken = SlotTaken(session)
        self._outbox = OutboxRepository(session)
        self._idempotency = IdempotencyRepository(session, ttl=idempotency_ttl)
        self._offerings = ReadOffering(catalog, cache)
        self._cache = cache
        self._clock = clock
        self._reminder_lead = reminder_lead

    async def execute(
        self, *, caller: Principal, request: IdempotentRequest, body: BookingCreateRequest
    ) -> BookingResponse:
        """Create the booking, or repeat the answer this key already got."""
        now = self._clock()
        # The key belongs to whoever sends the request: an admin repeating a
        # booking they made for a client gets their own answer back.
        caller_id = UserId(caller.user_id)
        client_user_id = (
            UserId(body.client_user_id) if body.client_user_id is not None else caller_id
        )
        if client_user_id == caller_id and not caller.holds("client"):
            # An admin's own haircut is booked with their client role, like
            # anybody's; without it the admin books only for others.
            raise NotAllowedForActor("Booking for yourself takes the client role")
        if client_user_id != caller_id and not may_book_for_others(caller):
            # Refused before catalog is asked: no salon would make it allowed.
            raise NotAllowedForActor("Only the salon may book on behalf of another client")
        master_id = MasterId(body.master_id)
        service_id = ServiceId(body.service_id)

        offering = await self._offerings.execute(master_id=master_id, service_id=service_id)
        if not offering.master_active:
            raise MasterInactive("This master takes no bookings")
        creator = booking_creator(
            caller, salon_id=SalonId(offering.salon_id), client_user_id=client_user_id
        )

        zone = ZoneInfo(offering.salon.timezone)
        validate_lead_time(body.start_at, now=now, lead_min=offering.salon.booking_min_lead_min)
        validate_horizon(
            body.start_at,
            now=now,
            horizon_days=offering.salon.booking_horizon_days,
            zone=zone,
        )

        try:
            created, response = await self._write(
                caller_id=caller_id,
                creator=creator,
                client_user_id=client_user_id,
                master_id=master_id,
                service_id=service_id,
                request=request,
                body=body,
                zone=zone,
                now=now,
                offering=offering,
            )
        except IntegrityError as error:
            if is_unique_violation(error, constraint=IDEMPOTENCY_CONSTRAINT):
                # A second request with the same key got here first. Its answer
                # is the one this caller asked for.
                return await self._answer_already_given(caller_id, request, now, error)
            if not is_overlap(error):
                # A unique violation is something else entirely, and reporting
                # it as a taken slot would hide a real defect.
                raise
            raise await self._slot_taken.execute(
                master_id=master_id,
                wanted=body.start_at,
                duration_min=offering.duration_min,
                offering=offering,
                now=now,
            ) from error

        if created:
            BOOKINGS_CREATED.labels(salon=str(response.salon_id), status=response.status).inc()
            # After the commit: the day of this master has changed, and the
            # cached availability of it must not outlive the booking.
            await self._cache.invalidate_master(master_id)
        return response

    async def _write(
        self,
        *,
        caller_id: UserId,
        creator: Actor,
        client_user_id: UserId,
        master_id: MasterId,
        service_id: ServiceId,
        request: IdempotentRequest,
        body: BookingCreateRequest,
        zone: ZoneInfo,
        now: datetime,
        offering: MasterServiceDetails,
    ) -> tuple[bool, BookingResponse]:
        """The transaction: the key, the row and the event, or none of them."""
        async with transaction(self._session):
            stored = await self._idempotency.find(user_id=caller_id, request=request, now=now)
            if stored is not None:
                return False, BookingResponse.model_validate(stored.body)

            settings = await self._masters.get(master_id)
            buffer_min = settings.buffer_after_min if settings is not None else 0
            validate_start_is_bookable(
                body.start_at,
                work=await self._working_time(master_id, body.start_at, zone),
                duration_min=offering.duration_min,
                buffer_min=buffer_min,
                step_min=offering.salon.slot_step_min,
            )

            booking = await self._bookings.add(
                _booking_of(
                    body=body,
                    client_user_id=client_user_id,
                    creator=creator,
                    buffer_min=buffer_min,
                    offering=offering,
                    reminder_at=reminder_for(body.start_at, now=now, lead=self._reminder_lead),
                )
            )
            response = booking_response(booking)
            await self._outbox.add(
                topic=BOOKINGS_TOPIC,
                aggregate_type=BOOKING_AGGREGATE_TYPE,
                aggregate_id=booking.id,
                event_type=BookingEventType.CREATED.value,
                payload=_event_of(booking),
            )
            await self._idempotency.remember(
                user_id=caller_id,
                request=request,
                response=StoredResponse(
                    status=CREATED_STATUS, body=response.model_dump(mode="json")
                ),
                now=now,
            )
        return True, response

    async def _working_time(
        self, master_id: MasterId, start_at: datetime, zone: ZoneInfo
    ) -> list[TimeRange]:
        """The master's working intervals on the salon's date of this start."""
        day = start_at.astimezone(zone).date()
        return working_intervals(
            day,
            await self._schedule.templates_from(master_id, day),
            await self._schedule.exceptions_on(master_id, day),
            zone,
        )

    async def _answer_already_given(
        self,
        caller_id: UserId,
        request: IdempotentRequest,
        now: datetime,
        error: IntegrityError,
    ) -> BookingResponse:
        """Read what the request that won the key stored."""
        async with transaction(self._session):
            stored = await self._idempotency.find(user_id=caller_id, request=request, now=now)
        if stored is None:  # pragma: no cover - the winner commits before it releases the key
            raise error
        return BookingResponse.model_validate(stored.body)


def _booking_of(
    *,
    body: BookingCreateRequest,
    client_user_id: UserId,
    creator: Actor,
    buffer_min: int,
    offering: MasterServiceDetails,
    reminder_at: datetime | None,
) -> Booking:
    return Booking.confirmed(
        id=BookingId(uuid4()),
        salon_id=SalonId(offering.salon_id),
        master_id=MasterId(body.master_id),
        client_user_id=client_user_id,
        # The snapshot: renaming the service or changing its price later does
        # not rewrite what this client booked.
        service=ServiceSnapshot(
            service_id=ServiceId(body.service_id),
            name=offering.service_name,
            price=offering.price,
            currency=offering.currency,
            duration_min=offering.duration_min,
        ),
        buffer_min=buffer_min,
        cancel_deadline_min=offering.salon.cancel_deadline_min,
        start_at=body.start_at,
        reminder_at=reminder_at,
        by=creator,
    )


def _event_of(booking: Booking) -> BookingCreated:
    return BookingCreated(
        booking_id=booking.id,
        salon_id=booking.salon_id,
        master_id=booking.master_id,
        client_user_id=booking.client_user_id,
        service_id=booking.service.service_id,
        service_name=booking.service.name,
        price=booking.service.price,
        currency=booking.service.currency,
        start_at=booking.start_at,
        end_at=booking.end_at,
        status=booking.status.value,
        reminder_at=booking.reminder_at,
        created_by=booking.created_by,
    )
