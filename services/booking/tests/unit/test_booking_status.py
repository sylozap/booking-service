"""The statuses of the domain and the ones the database spells out agree."""

from __future__ import annotations

from barber_booking.domain.booking_status import ACTIVE_STATUSES, BookingStatus
from barber_booking.models.booking import ACTIVE_BOOKING_STATUSES, BOOKING_STATUSES


def test_the_database_knows_every_status_of_the_domain() -> None:
    assert set(BOOKING_STATUSES) == {status.value for status in BookingStatus}


def test_the_exclusion_constraint_covers_exactly_the_active_statuses() -> None:
    assert set(ACTIVE_BOOKING_STATUSES) == {status.value for status in ACTIVE_STATUSES}


def test_a_cancellation_frees_the_time() -> None:
    assert BookingStatus.CANCELLED_BY_CLIENT not in ACTIVE_STATUSES
    assert BookingStatus.CANCELLED_BY_SALON not in ACTIVE_STATUSES
