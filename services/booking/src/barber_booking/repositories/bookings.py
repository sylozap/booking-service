"""Access to ``bookings``."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import ColumnElement, Select, false, or_, select, true, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from barber_booking.domain.booking import Booking, ServiceSnapshot
from barber_booking.domain.booking_status import BookingStatus
from barber_booking.domain.identifiers import BookingId, MasterId, SalonId, ServiceId, UserId
from barber_booking.domain.visibility import BookingFilters, BookingScope
from barber_booking.models.booking import OVERLAP_CONSTRAINT, occupied_range_of
from barber_booking.models.booking import Booking as BookingRow
from barber_booking.models.master_settings import MasterSettings as MasterSettingsRow
from barber_common.db.errors import (
    SQLSTATE_EXCLUSION_VIOLATION,
    constraint_name_of,
    sqlstate_of,
)
from barber_common.pagination import InvalidCursor, PageRequest, cursor_uuid

__all__ = ["BOOKING_CURSOR_ARITY", "BookingRepository", "booking_cursor", "is_overlap"]

# A listing is ordered by ``(start_at, id)``: the start is what a person reads
# a list of appointments by, and the id makes the order total.
BOOKING_CURSOR_ARITY = 2


def booking_cursor(booking: Booking) -> tuple[str, str]:
    """The sort key a page of bookings resumes after."""
    return booking.start_at.isoformat(), str(booking.id)


def is_overlap(error: IntegrityError) -> bool:
    """Whether the write lost to the exclusion constraint, and not to another rule.

    Both the SQLSTATE and the name are checked: a unique violation is something
    else entirely, and reporting it as a taken slot would hide a real defect.
    """
    return (
        sqlstate_of(error) == SQLSTATE_EXCLUSION_VIOLATION
        and constraint_name_of(error) == OVERLAP_CONSTRAINT
    )


def _to_domain(row: BookingRow) -> Booking:
    return Booking(
        id=BookingId(row.id),
        salon_id=SalonId(row.salon_id),
        master_id=MasterId(row.master_id),
        client_user_id=UserId(row.client_user_id),
        service=ServiceSnapshot(
            service_id=ServiceId(row.service_id),
            name=row.service_name,
            price=row.price,
            currency=row.currency,
            duration_min=row.duration_min,
        ),
        buffer_min=row.buffer_min,
        cancel_deadline_min=row.cancel_deadline_min,
        start_at=row.start_at,
        status=BookingStatus(row.status),
        created_by=UserId(row.created_by),
        cancelled_by=None if row.cancelled_by is None else UserId(row.cancelled_by),
        cancel_reason=row.cancel_reason,
        cancelled_at=row.cancelled_at,
        reminder_at=row.reminder_at,
        reminder_sent_at=row.reminder_sent_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_row(booking: Booking) -> BookingRow:
    return BookingRow(
        id=booking.id,
        salon_id=booking.salon_id,
        master_id=booking.master_id,
        client_user_id=booking.client_user_id,
        service_id=booking.service.service_id,
        service_name=booking.service.name,
        price=booking.service.price,
        currency=booking.service.currency,
        duration_min=booking.service.duration_min,
        buffer_min=booking.buffer_min,
        cancel_deadline_min=booking.cancel_deadline_min,
        start_at=booking.start_at,
        end_at=booking.end_at,
        status=booking.status.value,
        created_by=booking.created_by,
        reminder_at=booking.reminder_at,
    )


class BookingRepository:
    """Writing and reading bookings, inside the caller's transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, booking: Booking) -> Booking:
        """Stage a booking and let the database judge it.

        The flush is what makes the exclusion constraint speak: without it the
        conflict would surface at commit, outside the scenario that can answer
        it. The booking comes back with what the database filled in.
        """
        row = _to_row(booking)
        self._session.add(row)
        await self._session.flush()
        return _to_domain(row)

    async def get(self, booking_id: BookingId) -> Booking | None:
        row = await self._session.get(BookingRow, booking_id)
        return None if row is None else _to_domain(row)

    async def lock(self, booking_id: BookingId) -> Booking | None:
        """Read a booking and hold it until the transaction ends.

        Every change of a booking takes this lock first, so a cancellation and
        a completion arriving together cannot both decide from the same state.
        """
        statement = (
            select(BookingRow)
            .where(BookingRow.id == booking_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        row = (await self._session.execute(statement)).scalar_one_or_none()
        return None if row is None else _to_domain(row)

    async def save(self, booking: Booking) -> Booking:
        """Write what a change of state touched, on the same row.

        An ``UPDATE`` and never a delete with an insert: a moved booking keeps
        its identity and its history, and the exclusion constraint judges the
        new time against every other booking but this one.
        """
        statement = (
            update(BookingRow)
            .where(BookingRow.id == booking.id)
            .values(
                status=booking.status.value,
                start_at=booking.start_at,
                end_at=booking.end_at,
                buffer_min=booking.buffer_min,
                occupied_range=occupied_range_of(
                    booking.start_at, booking.end_at, booking.buffer_min
                ),
                cancelled_by=booking.cancelled_by,
                cancel_reason=booking.cancel_reason,
                cancelled_at=booking.cancelled_at,
                reminder_at=booking.reminder_at,
                reminder_sent_at=booking.reminder_sent_at,
            )
            .returning(BookingRow)
            .execution_options(populate_existing=True)
        )
        row = (await self._session.execute(statement)).scalar_one()
        return _to_domain(row)

    async def get_visible(self, booking_id: BookingId, scope: BookingScope) -> Booking | None:
        """One booking, if it exists and the caller may see it.

        Not existing and not being visible are one answer: a caller learns
        nothing about bookings that are not theirs.
        """
        statement = select(BookingRow).where(BookingRow.id == booking_id, _visible_to(scope))
        row = (await self._session.execute(statement)).scalar_one_or_none()
        return None if row is None else _to_domain(row)

    async def page(
        self, *, scope: BookingScope, filters: BookingFilters, request: PageRequest
    ) -> Sequence[Booking]:
        """One window of the bookings the caller may see, ordered by ``(start_at, id)``.

        The scope is part of the ``WHERE`` clause, never a pass over the rows
        afterwards: dropping invisible rows after the fetch would return short
        pages and a cursor that skips what it should have shown. Returns one
        row more than requested; the scenario turns it into ``next_cursor``.
        """
        statement = select(BookingRow).where(_visible_to(scope), *_narrowed_by(filters))
        statement = _resume(statement, request)
        result = await self._session.execute(
            statement.order_by(BookingRow.start_at, BookingRow.id).limit(request.window())
        )
        return [_to_domain(row) for row in result.scalars().all()]


def _visible_to(scope: BookingScope) -> ColumnElement[bool]:
    """The rows a caller may see, as one condition: the union of their roles."""
    if scope.sees_everything:
        return true()

    visible: list[ColumnElement[bool]] = [BookingRow.client_user_id == scope.user_id]
    if scope.admin_of:
        visible.append(BookingRow.salon_id.in_(scope.admin_of))
    if scope.is_master:
        # The masters this account is, in the salons where it holds the role.
        own_masters = select(MasterSettingsRow.master_id).where(
            MasterSettingsRow.user_id == scope.user_id
        )
        if not scope.master_anywhere:
            own_masters = own_masters.where(MasterSettingsRow.salon_id.in_(scope.master_in))
        visible.append(BookingRow.master_id.in_(own_masters))
    return or_(false(), *visible)


def _narrowed_by(filters: BookingFilters) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = []
    if filters.starts_from is not None:
        conditions.append(BookingRow.start_at >= filters.starts_from)
    if filters.starts_before is not None:
        conditions.append(BookingRow.start_at < filters.starts_before)
    if filters.master_id is not None:
        conditions.append(BookingRow.master_id == filters.master_id)
    if filters.salon_id is not None:
        conditions.append(BookingRow.salon_id == filters.salon_id)
    if filters.statuses:
        conditions.append(BookingRow.status.in_(sorted(filters.statuses)))
    return conditions


def _resume(
    statement: Select[tuple[BookingRow]], request: PageRequest
) -> Select[tuple[BookingRow]]:
    """Continue after the row the cursor names: ``(start_at, id) > (:start_at, :id)``."""
    key = request.key(arity=BOOKING_CURSOR_ARITY)
    if key is None:
        return statement

    start_at, booking_id = key
    return statement.where(
        tuple_(BookingRow.start_at, BookingRow.id)
        > (_cursor_moment(start_at), cursor_uuid(booking_id))
    )


def _cursor_moment(value: str) -> datetime:
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as error:
        raise InvalidCursor("The cursor does not belong to this listing") from error
    if moment.tzinfo is None:
        raise InvalidCursor("The cursor does not belong to this listing")
    return moment
