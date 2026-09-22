"""Weekly templates and the exceptions of single dates."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from barber_booking.domain.identifiers import ExceptionId, MasterId
from barber_booking.domain.schedule import (
    ExceptionKind,
    ScheduleException,
    TemplateInterval,
    TimeWindow,
)
from barber_booking.models.schedule_exception import ScheduleException as ExceptionRow
from barber_booking.models.schedule_template import ScheduleTemplate as TemplateRow

__all__ = ["ScheduleRepository"]


def _template(row: TemplateRow) -> TemplateInterval:
    return TemplateInterval(
        weekday=row.weekday,
        window=TimeWindow(start=row.start_time, end=row.end_time),
        valid_from=row.valid_from,
        valid_to=row.valid_to,
    )


def _exception(row: ExceptionRow) -> ScheduleException:
    start, end = row.start_time, row.end_time
    return ScheduleException(
        id=ExceptionId(row.id),
        effective_on=row.effective_on,
        kind=ExceptionKind(row.kind),
        window=TimeWindow(start=start, end=end) if start is not None and end is not None else None,
        reason=row.reason,
    )


class ScheduleRepository:
    """Access to ``schedule_templates`` and ``schedule_exceptions``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def templates_from(self, master_id: MasterId, day: date) -> list[TemplateInterval]:
        """Every template line still in force on or after ``day``."""
        statement = (
            select(TemplateRow)
            .where(
                TemplateRow.master_id == master_id,
                or_(TemplateRow.valid_to.is_(None), TemplateRow.valid_to >= day),
            )
            .order_by(
                TemplateRow.valid_from,
                TemplateRow.weekday,
                TemplateRow.start_time,
            )
        )
        rows = (await self._session.execute(statement)).scalars().all()
        return [_template(row) for row in rows]

    async def replace_from(
        self, master_id: MasterId, valid_from: date, lines: Sequence[TemplateInterval]
    ) -> None:
        """Make ``lines`` the template from ``valid_from`` on.

        Versions starting on or after that date are dropped, the one in force
        is closed the day before, and history before the date stays as it was.
        """
        await self._session.execute(
            delete(TemplateRow).where(
                TemplateRow.master_id == master_id, TemplateRow.valid_from >= valid_from
            )
        )
        await self._session.execute(
            update(TemplateRow)
            .where(
                TemplateRow.master_id == master_id,
                or_(TemplateRow.valid_to.is_(None), TemplateRow.valid_to >= valid_from),
            )
            .values(valid_to=valid_from - timedelta(days=1))
        )
        self._session.add_all(
            TemplateRow(
                master_id=master_id,
                weekday=line.weekday,
                start_time=line.window.start,
                end_time=line.window.end,
                valid_from=line.valid_from,
                valid_to=line.valid_to,
            )
            for line in lines
        )
        await self._session.flush()

    async def exceptions_on(self, master_id: MasterId, day: date) -> list[ScheduleException]:
        return await self.exceptions_between(master_id, day, day)

    async def exceptions_between(
        self, master_id: MasterId, first: date, last: date
    ) -> list[ScheduleException]:
        """The exceptions of the dates from ``first`` to ``last``, both included."""
        statement = (
            select(ExceptionRow)
            .where(
                ExceptionRow.master_id == master_id,
                ExceptionRow.effective_on >= first,
                ExceptionRow.effective_on <= last,
            )
            .order_by(ExceptionRow.effective_on, ExceptionRow.start_time, ExceptionRow.id)
        )
        rows = (await self._session.execute(statement)).scalars().all()
        return [_exception(row) for row in rows]

    async def get_exception(
        self, master_id: MasterId, exception_id: ExceptionId
    ) -> ScheduleException | None:
        """One exception, only if it belongs to this master."""
        row = await self._session.get(ExceptionRow, exception_id)
        if row is None or row.master_id != master_id:
            return None
        return _exception(row)

    async def add_exception(
        self, master_id: MasterId, exception: ScheduleException
    ) -> ScheduleException:
        row = ExceptionRow(
            master_id=master_id,
            effective_on=exception.effective_on,
            kind=exception.kind.value,
            start_time=exception.window.start if exception.window else None,
            end_time=exception.window.end if exception.window else None,
            reason=exception.reason,
        )
        self._session.add(row)
        await self._session.flush()
        return _exception(row)

    async def remove_exception(self, exception_id: ExceptionId) -> None:
        await self._session.execute(delete(ExceptionRow).where(ExceptionRow.id == exception_id))
