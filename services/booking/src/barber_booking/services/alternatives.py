"""The answer to losing a race for a time: somewhere else to go.

Creating a booking and moving one both lose to the exclusion constraint in the
same way, and both answer with a few free starts from the moment that was
asked for.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from barber_booking.domain.errors import SlotAlreadyTaken
from barber_booking.domain.identifiers import BookingId, MasterId
from barber_booking.metrics import BOOKING_CONFLICTS
from barber_booking.repositories.availability import AvailabilityRepository
from barber_booking.repositories.master_settings import MasterSettingsRepository
from barber_common.contracts.catalog import MasterServiceDetails
from barber_common.db.session import transaction

__all__ = ["ALTERNATIVES_DAYS", "ALTERNATIVES_LIMIT", "SlotTaken", "horizon_ends"]

# How many free starts a conflict offers instead, and how far ahead they are
# looked for. Three is what a client can choose between; a week is long enough
# that a busy evening still has an answer.
ALTERNATIVES_LIMIT = 3
ALTERNATIVES_DAYS = 7


class SlotTaken:
    """Build ``slot_taken`` for a lost race, alternatives included."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._masters = MasterSettingsRepository(session)
        self._availability = AvailabilityRepository(session)

    async def execute(
        self,
        *,
        master_id: MasterId,
        wanted: datetime,
        duration_min: int,
        offering: MasterServiceDetails,
        now: datetime,
        ignoring: BookingId | None = None,
    ) -> SlotAlreadyTaken:
        """The error to raise, counted as a conflict.

        Read after the rollback, so the alternatives already account for the
        booking that won. An empty list is a valid answer -- the week ahead can
        genuinely be full -- and the client is told no more than that.

        ``ignoring`` is the booking being moved: its own current time is not
        in its way.
        """
        BOOKING_CONFLICTS.labels(salon=str(offering.salon_id)).inc()
        alternatives = await self._nearest_free(
            master_id=master_id,
            wanted=wanted,
            duration_min=duration_min,
            offering=offering,
            now=now,
            ignoring=ignoring,
        )
        return SlotAlreadyTaken(
            "This time has just been taken",
            extra={"alternatives": [start.isoformat() for start in alternatives]},
        )

    async def _nearest_free(
        self,
        *,
        master_id: MasterId,
        wanted: datetime,
        duration_min: int,
        offering: MasterServiceDetails,
        now: datetime,
        ignoring: BookingId | None,
    ) -> list[datetime]:
        """A few free starts from the requested moment on.

        The same query availability answers with, so the client is offered
        exactly what it would see if it asked again.
        """
        zone = ZoneInfo(offering.salon.timezone)
        first_day = wanted.astimezone(zone).date()
        async with transaction(self._session):
            settings = await self._masters.get(master_id)
            slots = await self._availability.slots(
                master_id=master_id,
                date_from=first_day,
                date_to=first_day + timedelta(days=ALTERNATIVES_DAYS),
                timezone=offering.salon.timezone,
                duration_min=duration_min,
                buffer_min=settings.buffer_after_min if settings is not None else 0,
                step_min=offering.salon.slot_step_min,
                # From the moment that was asked for, never earlier: a client
                # who wanted the evening is not offered the morning.
                not_before=max(
                    wanted, now + timedelta(minutes=offering.salon.booking_min_lead_min)
                ),
                not_after=horizon_ends(offering, now),
                ignoring=ignoring,
            )

        ordered = [start for day in sorted(slots) for start in slots[day]]
        return ordered[:ALTERNATIVES_LIMIT]


def horizon_ends(offering: MasterServiceDetails, now: datetime) -> datetime:
    """The instant the salon's booking horizon closes."""
    zone = ZoneInfo(offering.salon.timezone)
    last_date = now.astimezone(zone).date() + timedelta(days=offering.salon.booking_horizon_days)
    return datetime.combine(last_date + timedelta(days=1), time(), tzinfo=zone)
