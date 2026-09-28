"""Scoped listings of bookings, on the real database."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from barber_booking.domain.booking import Booking as BookingEntity
from barber_booking.domain.identifiers import BookingId, SalonId, UserId
from barber_booking.domain.visibility import BookingFilters, BookingScope
from barber_booking.models.booking import Booking
from barber_booking.repositories.bookings import BookingRepository, booking_cursor
from barber_common.db.session import transaction
from barber_common.pagination import InvalidCursor, Page, PageRequest, encode_cursor

pytestmark = pytest.mark.integration

BookingFactory = Callable[..., Awaitable[Booking]]

MORNING = datetime(2030, 3, 4, 7, 0, tzinfo=UTC)


async def walk(
    session: AsyncSession, scope: BookingScope, *, limit: int
) -> list[list[BookingEntity]]:
    """Every page of a listing, following the cursors to the end."""
    repository = BookingRepository(session)
    pages: list[list[BookingEntity]] = []
    cursor: str | None = None
    while True:
        request = PageRequest(limit=limit, cursor=cursor)
        async with transaction(session):
            rows = await repository.page(scope=scope, filters=BookingFilters(), request=request)
        page = Page.of(rows, request=request, cursor_of=booking_cursor)
        pages.append(list(page.items))
        if page.next_cursor is None:
            return pages
        cursor = page.next_cursor


async def test_pages_are_full_although_most_rows_are_not_visible(
    session: AsyncSession, make_booking: BookingFactory
) -> None:
    client = uuid4()
    mine: list[UUID] = []
    for hour in range(10):
        # Every third booking is this client's; the rest belong to strangers
        # and sit between them in the order of the listing.
        owner = client if hour % 3 == 0 else uuid4()
        booking = await make_booking(
            master_id=uuid4(), start_at=MORNING + timedelta(hours=hour), client_user_id=owner
        )
        if owner == client:
            mine.append(booking.id)

    pages = await walk(session, BookingScope(user_id=UserId(client)), limit=2)

    assert [len(page) for page in pages] == [2, 2]
    assert [booking.id for page in pages for booking in page] == mine


async def test_bookings_starting_together_are_each_listed_once(
    session: AsyncSession, make_booking: BookingFactory
) -> None:
    client = uuid4()
    created = {
        (await make_booking(master_id=uuid4(), start_at=MORNING, client_user_id=client)).id
        for _ in range(5)
    }

    pages = await walk(session, BookingScope(user_id=UserId(client)), limit=2)

    listed = [booking.id for page in pages for booking in page]
    assert sorted(listed) == sorted(created)
    assert [len(page) for page in pages] == [2, 2, 1]


async def test_a_booking_outside_the_scope_is_not_read(
    session: AsyncSession, make_booking: BookingFactory
) -> None:
    booking = await make_booking(master_id=uuid4(), start_at=MORNING)
    repository = BookingRepository(session)

    async with transaction(session):
        seen = await repository.get_visible(
            BookingId(booking.id), BookingScope(user_id=UserId(uuid4()))
        )
        by_admin = await repository.get_visible(
            BookingId(booking.id),
            BookingScope(user_id=UserId(uuid4()), admin_of=frozenset({SalonId(booking.salon_id)})),
        )

    assert seen is None
    assert by_admin is not None


async def test_a_cursor_with_a_start_that_is_not_an_instant_is_refused(
    session: AsyncSession,
) -> None:
    request = PageRequest(cursor=encode_cursor(["yesterday", str(uuid4())]))

    with pytest.raises(InvalidCursor):
        async with transaction(session):
            await BookingRepository(session).page(
                scope=BookingScope(user_id=UserId(uuid4())),
                filters=BookingFilters(),
                request=request,
            )
