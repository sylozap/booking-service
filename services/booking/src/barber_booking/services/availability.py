"""Answering "when is this master free for this service".

The hot path of the platform. What it costs is one cached call to ``catalog``,
one read of the master's settings, and one query per range of dates that is not
already in the cache.

The answer is an estimate and says so: between reading it and creating a
booking the slot may be taken, and the only arbiter of that is the constraint
in the database.
"""

from __future__ import annotations

import time as clock
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from barber_booking.clients.catalog import CatalogClient
from barber_booking.domain.identifiers import MasterId, ServiceId
from barber_booking.metrics import AVAILABILITY_DURATION, AvailabilitySource
from barber_booking.repositories.availability import AvailabilityRepository
from barber_booking.repositories.master_settings import MasterSettingsRepository
from barber_booking.services.cache import BookingCache
from barber_booking.services.clock import Clock, utc_now
from barber_booking.services.offerings import ReadOffering
from barber_common.contracts.booking import AvailabilityResponse, DayAvailability
from barber_common.contracts.catalog import MasterServiceDetails
from barber_common.db.session import transaction
from barber_common.errors import ValidationFailed

__all__ = ["MAX_WINDOW_DAYS", "ReadAvailability"]

# The widest window one request may ask for. Every extra day is a day of grid
# rows in the query, and a calendar shows a fortnight at a time.
MAX_WINDOW_DAYS = 14

DEFAULT_CACHE_TTL_SECONDS = 60


class ReadAvailability:
    """Free starts of one master for one service, day by day."""

    def __init__(
        self,
        session: AsyncSession,
        cache: BookingCache,
        catalog: CatalogClient,
        *,
        clock: Clock = utc_now,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
    ) -> None:
        self._session = session
        self._masters = MasterSettingsRepository(session)
        self._availability = AvailabilityRepository(session)
        self._cache = cache
        self._offerings = ReadOffering(catalog, cache)
        self._clock = clock
        self._cache_ttl_seconds = cache_ttl_seconds

    async def execute(
        self,
        *,
        master_id: MasterId,
        service_id: ServiceId,
        date_from: date,
        date_to: date | None,
    ) -> AvailabilityResponse:
        """Answer for every date of the window, empty days included.

        The call to ``catalog`` happens before any transaction opens: it is a
        network call, and holding a pooled connection across one is forbidden.
        """
        started = clock.monotonic()
        last = date_to or date_from
        _validate_window(date_from, last)

        offering = await self._offerings.execute(master_id=master_id, service_id=service_id)
        zone = ZoneInfo(offering.salon.timezone)
        now = self._clock()

        days = _window(date_from, last)
        if not offering.master_active:
            return _response(offering, {}, days)

        settings = await self._masters.get(master_id)
        if settings is None:
            # catalog knows the master, booking has no schedule for them yet:
            # no working time, so nothing is free.
            return _response(offering, {}, days)

        bounded = [day for day in days if _within_horizon(day, offering, now, zone)]
        slots, queried = await self._slots(
            master_id=master_id,
            service_id=service_id,
            days=bounded,
            offering=offering,
            buffer_min=settings.buffer_after_min,
            now=now,
            zone=zone,
        )
        source = AvailabilitySource.DATABASE if queried else AvailabilitySource.CACHE
        AVAILABILITY_DURATION.labels(source=source).observe(clock.monotonic() - started)
        return _response(offering, slots, days)

    async def _slots(
        self,
        *,
        master_id: MasterId,
        service_id: ServiceId,
        days: list[date],
        offering: MasterServiceDetails,
        buffer_min: int,
        now: datetime,
        zone: ZoneInfo,
    ) -> tuple[dict[date, list[datetime]], bool]:
        """Read what is cached and compute the rest in one query.

        Also reports whether the database was read at all, which is what the
        duration is labelled by.
        """
        found: dict[date, list[datetime]] = {}
        missing: list[date] = []
        for day in days:
            key = await self._cache.availability_key(master_id, service_id, day)
            cached = await self._cache.read(key, DayAvailability)
            if cached is None:
                missing.append(day)
            else:
                found[day] = cached.slots

        if not missing:
            return found, False

        # One query over the span the misses cover, rather than one per day:
        # the days in between are usually missing too, and a second round trip
        # costs more than the rows.
        computed = await self._query(
            master_id=master_id,
            first=missing[0],
            last=missing[-1],
            offering=offering,
            buffer_min=buffer_min,
            now=now,
            zone=zone,
        )
        for day in missing:
            slots = computed.get(day, [])
            found[day] = slots
            key = await self._cache.availability_key(master_id, service_id, day)
            await self._cache.write(
                key,
                DayAvailability(date=day, slots=slots),
                ttl_seconds=self._cache_ttl_seconds,
            )
        return found, True

    async def _query(
        self,
        *,
        master_id: MasterId,
        first: date,
        last: date,
        offering: MasterServiceDetails,
        buffer_min: int,
        now: datetime,
        zone: ZoneInfo,
    ) -> dict[date, list[datetime]]:
        async with transaction(self._session):
            return await self._availability.slots(
                master_id=master_id,
                date_from=first,
                date_to=last,
                timezone=offering.salon.timezone,
                duration_min=offering.duration_min,
                buffer_min=buffer_min,
                step_min=offering.salon.slot_step_min,
                not_before=now + timedelta(minutes=offering.salon.booking_min_lead_min),
                not_after=_horizon_ends(offering, now, zone),
            )


def _validate_window(first: date, last: date) -> None:
    if last < first:
        raise ValidationFailed("date_to cannot be before date_from")
    if (last - first).days + 1 > MAX_WINDOW_DAYS:
        raise ValidationFailed(f"The window cannot be longer than {MAX_WINDOW_DAYS} days")


def _window(first: date, last: date) -> list[date]:
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


def _last_bookable_date(offering: MasterServiceDetails, now: datetime, zone: ZoneInfo) -> date:
    """The last date of the salon's calendar a client may still book."""
    today = now.astimezone(zone).date()
    return today + timedelta(days=offering.salon.booking_horizon_days)


def _within_horizon(
    day: date, offering: MasterServiceDetails, now: datetime, zone: ZoneInfo
) -> bool:
    return day <= _last_bookable_date(offering, now, zone)


def _horizon_ends(offering: MasterServiceDetails, now: datetime, zone: ZoneInfo) -> datetime:
    """The instant the horizon closes: local midnight after its last date."""
    after = _last_bookable_date(offering, now, zone) + timedelta(days=1)
    return datetime.combine(after, time(), tzinfo=zone).astimezone(now.tzinfo)


def _response(
    offering: MasterServiceDetails,
    slots: dict[date, list[datetime]],
    days: list[date],
) -> AvailabilityResponse:
    return AvailabilityResponse(
        master_id=offering.master_id,
        service_id=offering.service_id,
        timezone=offering.salon.timezone,
        master_active=offering.master_active,
        duration_min=offering.duration_min,
        days=[DayAvailability(date=day, slots=slots.get(day, [])) for day in days],
    )
