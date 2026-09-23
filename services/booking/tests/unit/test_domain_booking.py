"""A booking as the domain holds it."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from barber_booking.domain.booking import Booking, ServiceSnapshot
from barber_booking.domain.booking_status import BookingStatus
from barber_booking.domain.identifiers import BookingId, MasterId, SalonId, ServiceId, UserId
from barber_booking.domain.time_range import TimeRange

START = datetime(2026, 10, 5, 7, 0, tzinfo=UTC)
CLIENT = UserId(uuid4())


def a_booking(
    *,
    start_at: datetime = START,
    buffer_min: int = 0,
    cancel_deadline_min: int = 240,
) -> Booking:
    """A confirmed 45-minute booking; a test bends the fields it is about."""
    return Booking.confirmed(
        id=BookingId(uuid4()),
        salon_id=SalonId(uuid4()),
        master_id=MasterId(uuid4()),
        client_user_id=CLIENT,
        service=ServiceSnapshot(
            service_id=ServiceId(uuid4()),
            name="Haircut",
            price=Decimal("3500.00"),
            currency="RUB",
            duration_min=45,
        ),
        buffer_min=buffer_min,
        cancel_deadline_min=cancel_deadline_min,
        start_at=start_at,
        reminder_at=None,
    )


def test_a_new_booking_is_confirmed_at_once_and_made_by_its_client() -> None:
    booking = a_booking()

    assert booking.status is BookingStatus.CONFIRMED
    assert booking.created_by == CLIENT


def test_a_booking_ends_when_its_service_does() -> None:
    booking = a_booking(buffer_min=15)

    assert booking.end_at == START + timedelta(minutes=45)


def test_the_buffer_keeps_the_master_occupied_after_the_service() -> None:
    booking = a_booking(buffer_min=15)

    assert booking.occupied == TimeRange(START, START + timedelta(minutes=60))


def test_a_booking_without_a_time_zone_is_refused() -> None:
    with pytest.raises(ValueError, match="time zone"):
        a_booking(start_at=START.replace(tzinfo=None))


def test_a_negative_buffer_is_refused() -> None:
    with pytest.raises(ValueError, match="buffer"):
        a_booking(buffer_min=-5)


def test_a_negative_cancellation_deadline_is_refused() -> None:
    with pytest.raises(ValueError, match="deadline"):
        a_booking(cancel_deadline_min=-1)


def test_a_service_without_a_duration_is_refused() -> None:
    with pytest.raises(ValueError, match="duration"):
        replace(a_booking().service, duration_min=0)
