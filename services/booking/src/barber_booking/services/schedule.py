"""Setting and reading a master's schedule.

Every change takes the lock on the master's settings row first, validates
against what is stored, writes, commits, and only then retires the cached
availability of that master. **A change never cancels bookings**, even those
it leaves outside working time: an administrator sees the conflict and settles
it by hand.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from itertools import groupby

from sqlalchemy.ext.asyncio import AsyncSession

from barber_booking.domain.errors import MasterNotFound
from barber_booking.domain.identifiers import ExceptionId, MasterId
from barber_booking.domain.master import MasterSettings
from barber_booking.domain.schedule import (
    ScheduleException,
    TemplateInterval,
    TimeWindow,
    validate_day_exceptions,
    validate_not_in_past,
    validate_weekly_template,
)
from barber_booking.repositories.master_settings import MasterSettingsRepository
from barber_booking.repositories.schedule import ScheduleRepository
from barber_booking.schemas.schedule import (
    ScheduleExceptionList,
    ScheduleExceptionRequest,
    ScheduleExceptionResponse,
    ScheduleVersion,
    WeeklyScheduleRequest,
    WeeklyScheduleResponse,
    WorkingInterval,
)
from barber_booking.services.authorization import require_schedule_access
from barber_booking.services.cache import BookingCache
from barber_booking.services.clock import Clock, utc_now
from barber_common.auth import Principal
from barber_common.db.session import transaction
from barber_common.errors import NotFound, ValidationFailed

__all__ = [
    "AddScheduleException",
    "ListScheduleExceptions",
    "ReadWeeklySchedule",
    "RemoveScheduleException",
    "ReplaceWeeklySchedule",
]

# The widest window of exceptions one request lists. A bound rather than a
# cursor: the dates are the natural pages, and a year is small.
MAX_EXCEPTION_WINDOW = timedelta(days=366)
DEFAULT_EXCEPTION_WINDOW = timedelta(days=92)


def exception_response(exception: ScheduleException) -> ScheduleExceptionResponse:
    if exception.id is None:  # pragma: no cover - only stored exceptions are shown
        raise ValueError("an exception is shown only once it is stored")
    return ScheduleExceptionResponse(
        id=exception.id,
        effective_on=exception.effective_on,
        kind=exception.kind,
        start_time=exception.window.start if exception.window else None,
        end_time=exception.window.end if exception.window else None,
        reason=exception.reason,
    )


def schedule_response(
    master: MasterSettings, lines: Sequence[TemplateInterval]
) -> WeeklyScheduleResponse:
    """Group template lines into the versions they belong to."""

    def version_of(line: TemplateInterval) -> tuple[date, date | None]:
        return (line.valid_from, line.valid_to)

    versions = [
        ScheduleVersion(
            valid_from=valid_from,
            valid_to=valid_to,
            intervals=[
                WorkingInterval(
                    weekday=line.weekday,
                    start_time=line.window.start,
                    end_time=line.window.end,
                )
                for line in group
            ],
        )
        for (valid_from, valid_to), group in groupby(lines, key=version_of)
    ]
    return WeeklyScheduleResponse(
        master_id=master.master_id, timezone=master.timezone, versions=versions
    )


class _ScheduleScenario:
    """What every schedule scenario is built from."""

    def __init__(self, session: AsyncSession, cache: BookingCache, clock: Clock = utc_now) -> None:
        self._session = session
        self._masters = MasterSettingsRepository(session)
        self._schedule = ScheduleRepository(session)
        self._cache = cache
        self._clock = clock

    async def _manageable(
        self, caller: Principal, master_id: MasterId, *, lock: bool
    ) -> MasterSettings:
        """Load the master and check the caller may manage them."""
        master = await (self._masters.lock(master_id) if lock else self._masters.get(master_id))
        if master is None:
            raise MasterNotFound("No such master")
        require_schedule_access(caller, master)
        return master


class ReplaceWeeklySchedule(_ScheduleScenario):
    """Put a new weekly template in force from a date on."""

    async def execute(
        self, *, caller: Principal, master_id: MasterId, body: WeeklyScheduleRequest
    ) -> WeeklyScheduleResponse:
        """Replace the template from ``valid_from``; exceptions are not touched."""
        async with transaction(self._session):
            master = await self._manageable(caller, master_id, lock=True)
            today = master.today(self._clock())
            valid_from = body.valid_from or today
            validate_not_in_past(valid_from, today=today)

            lines = [
                TemplateInterval(
                    weekday=interval.weekday,
                    window=TimeWindow(start=interval.start_time, end=interval.end_time),
                    valid_from=valid_from,
                )
                for interval in body.intervals
            ]
            validate_weekly_template(lines)

            await self._schedule.replace_from(master_id, valid_from, lines)
            current = await self._schedule.templates_from(master_id, today)

        await self._cache.invalidate_master(master_id)
        return schedule_response(master, current)


class ReadWeeklySchedule(_ScheduleScenario):
    """The template in force today and the versions planned after it."""

    async def execute(self, *, caller: Principal, master_id: MasterId) -> WeeklyScheduleResponse:
        async with transaction(self._session):
            master = await self._manageable(caller, master_id, lock=False)
            lines = await self._schedule.templates_from(master_id, master.today(self._clock()))
        return schedule_response(master, lines)


class AddScheduleException(_ScheduleScenario):
    """Mark one date as a day off, other hours or a break."""

    async def execute(
        self, *, caller: Principal, master_id: MasterId, body: ScheduleExceptionRequest
    ) -> ScheduleExceptionResponse:
        async with transaction(self._session):
            master = await self._manageable(caller, master_id, lock=True)
            validate_not_in_past(body.effective_on, today=master.today(self._clock()))

            exception = ScheduleException(
                effective_on=body.effective_on,
                kind=body.kind,
                window=TimeWindow(start=body.start_time, end=body.end_time)
                if body.start_time is not None and body.end_time is not None
                else None,
                reason=body.reason,
            )
            existing = await self._schedule.exceptions_on(master_id, body.effective_on)
            validate_day_exceptions([*existing, exception])

            stored = await self._schedule.add_exception(master_id, exception)

        await self._cache.invalidate_master(master_id)
        return exception_response(stored)


class ListScheduleExceptions(_ScheduleScenario):
    """The exceptions of a window of dates."""

    async def execute(
        self,
        *,
        caller: Principal,
        master_id: MasterId,
        date_from: date | None,
        date_to: date | None,
    ) -> ScheduleExceptionList:
        async with transaction(self._session):
            master = await self._manageable(caller, master_id, lock=False)
            first = date_from or master.today(self._clock())
            last = date_to or first + DEFAULT_EXCEPTION_WINDOW
            if last < first:
                raise ValidationFailed("date_to cannot be before date_from")
            if last - first > MAX_EXCEPTION_WINDOW:
                raise ValidationFailed("The window of dates cannot be longer than a year")

            exceptions = await self._schedule.exceptions_between(master_id, first, last)
        return ScheduleExceptionList(items=[exception_response(item) for item in exceptions])


class RemoveScheduleException(_ScheduleScenario):
    """Withdraw an exception that has not happened yet."""

    async def execute(
        self, *, caller: Principal, master_id: MasterId, exception_id: ExceptionId
    ) -> None:
        async with transaction(self._session):
            master = await self._manageable(caller, master_id, lock=True)
            exception = await self._schedule.get_exception(master_id, exception_id)
            if exception is None:
                raise NotFound("No such exception for this master")
            # The past stays as it was: it explains why nobody was booked then.
            validate_not_in_past(exception.effective_on, today=master.today(self._clock()))

            await self._schedule.remove_exception(exception_id)

        await self._cache.invalidate_master(master_id)
