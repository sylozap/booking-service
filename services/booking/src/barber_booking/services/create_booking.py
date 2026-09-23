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
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from barber_booking.clients.catalog import CatalogClient
from barber_booking.domain.booking_status import BookingStatus
from barber_booking.domain.errors import MasterInactive, SlotAlreadyTaken
from barber_booking.domain.identifiers import MasterId, ServiceId, UserId
from barber_booking.domain.policies import (
    validate_horizon,
    validate_lead_time,
    validate_start_is_bookable,
)
from barber_booking.domain.schedule import working_intervals
from barber_booking.domain.time_range import TimeRange
from barber_booking.models.booking import OVERLAP_CONSTRAINT, Booking
from barber_booking.repositories.bookings import BookingRepository
from barber_booking.repositories.master_settings import MasterSettingsRepository
from barber_booking.repositories.schedule import ScheduleRepository
from barber_booking.schemas.bookings import BookingCreateRequest, BookingResponse
from barber_booking.services.cache import BookingCache
from barber_booking.services.clock import Clock, utc_now
from barber_booking.services.offerings import ReadOffering
from barber_common.auth import Principal
from barber_common.contracts.catalog import MasterServiceDetails
from barber_common.db.errors import (
    SQLSTATE_EXCLUSION_VIOLATION,
    constraint_name_of,
    is_unique_violation,
    sqlstate_of,
)
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
        client_user_id = UserId(caller.user_id)
        master_id = MasterId(body.master_id)
        service_id = ServiceId(body.service_id)

        offering = await self._offerings.execute(master_id=master_id, service_id=service_id)
        if not offering.master_active:
            raise MasterInactive("This master takes no bookings")

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
                return await self._answer_already_given(client_user_id, request, now, error)
            raise self._translated(error) from error

        if created:
            # After the commit: the day of this master has changed, and the
            # cached availability of it must not outlive the booking.
            await self._cache.invalidate_master(master_id)
        return response

    async def _write(
        self,
        *,
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
            stored = await self._idempotency.find(user_id=client_user_id, request=request, now=now)
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
                    buffer_min=buffer_min,
                    offering=offering,
                    reminder_at=self._reminder_for(body.start_at, now),
                )
            )
            response = _response(booking)
            await self._outbox.add(
                topic=BOOKINGS_TOPIC,
                aggregate_type=BOOKING_AGGREGATE_TYPE,
                aggregate_id=booking.id,
                event_type=BookingEventType.CREATED.value,
                payload=_event_of(booking),
            )
            await self._idempotency.remember(
                user_id=client_user_id,
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

    def _reminder_for(self, start_at: datetime, now: datetime) -> datetime | None:
        """When to remind the client, unless that moment has already passed."""
        reminder_at = start_at - self._reminder_lead
        return reminder_at if reminder_at > now else None

    async def _answer_already_given(
        self,
        client_user_id: UserId,
        request: IdempotentRequest,
        now: datetime,
        error: IntegrityError,
    ) -> BookingResponse:
        """Read what the request that won the key stored."""
        async with transaction(self._session):
            stored = await self._idempotency.find(user_id=client_user_id, request=request, now=now)
        if stored is None:  # pragma: no cover - the winner commits before it releases the key
            raise error
        return BookingResponse.model_validate(stored.body)

    def _translated(self, error: IntegrityError) -> Exception:
        """Turn the one violation that has a domain meaning into its error.

        Only this one: a unique violation is something else entirely, and
        reporting it as a taken slot would hide a real defect.
        """
        if (
            sqlstate_of(error) == SQLSTATE_EXCLUSION_VIOLATION
            and constraint_name_of(error) == OVERLAP_CONSTRAINT
        ):
            return SlotAlreadyTaken("This time has just been taken")
        return error


def _booking_of(
    *,
    body: BookingCreateRequest,
    client_user_id: UserId,
    buffer_min: int,
    offering: MasterServiceDetails,
    reminder_at: datetime | None,
) -> Booking:
    return Booking(
        salon_id=offering.salon_id,
        master_id=body.master_id,
        client_user_id=client_user_id,
        service_id=body.service_id,
        # The snapshot: renaming the service or changing its price later does
        # not rewrite what this client booked.
        service_name=offering.service_name,
        price=offering.price,
        currency=offering.currency,
        duration_min=offering.duration_min,
        buffer_min=buffer_min,
        start_at=body.start_at,
        end_at=body.start_at + timedelta(minutes=offering.duration_min),
        # Confirmed at once: pending exists for a prepayment step the platform
        # does not have yet.
        status=BookingStatus.CONFIRMED.value,
        created_by=client_user_id,
        reminder_at=reminder_at,
    )


def _response(booking: Booking) -> BookingResponse:
    return BookingResponse(
        id=booking.id,
        salon_id=booking.salon_id,
        master_id=booking.master_id,
        client_user_id=booking.client_user_id,
        service_id=booking.service_id,
        service_name=booking.service_name,
        price=booking.price,
        currency=booking.currency,
        duration_min=booking.duration_min,
        buffer_min=booking.buffer_min,
        start_at=booking.start_at,
        end_at=booking.end_at,
        status=booking.status,
        reminder_at=booking.reminder_at,
        created_at=booking.created_at,
    )


def _event_of(booking: Booking) -> BookingCreated:
    return BookingCreated(
        booking_id=booking.id,
        salon_id=booking.salon_id,
        master_id=booking.master_id,
        client_user_id=booking.client_user_id,
        service_id=booking.service_id,
        service_name=booking.service_name,
        price=booking.price,
        currency=booking.currency,
        start_at=booking.start_at,
        end_at=booking.end_at,
        status=booking.status,
        reminder_at=booking.reminder_at,
    )
