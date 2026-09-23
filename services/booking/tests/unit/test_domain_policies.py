"""The windows a booking has to fall inside."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from barber_booking.domain.errors import BookingTooFar, BookingTooLate, SlotOutsideSchedule
from barber_booking.domain.policies import (
    validate_horizon,
    validate_lead_time,
    validate_start_is_bookable,
)
from barber_booking.domain.time_range import TimeRange

MOSCOW = ZoneInfo("Europe/Moscow")

NOW = datetime(2026, 10, 5, 6, 0, tzinfo=UTC)
TEN_O_CLOCK = datetime(2026, 10, 5, 7, 0, tzinfo=UTC)
WORKING = [TimeRange(TEN_O_CLOCK, TEN_O_CLOCK + timedelta(hours=3))]


# --- the lead time ----------------------------------------------------------


def test_a_start_far_enough_ahead_is_allowed() -> None:
    validate_lead_time(TEN_O_CLOCK, now=NOW, lead_min=30)


def test_a_start_inside_the_lead_time_is_refused() -> None:
    with pytest.raises(BookingTooLate) as failure:
        validate_lead_time(TEN_O_CLOCK, now=NOW, lead_min=120)

    assert failure.value.code == "booking_too_late"


def test_a_start_exactly_at_the_lead_time_is_allowed() -> None:
    validate_lead_time(TEN_O_CLOCK, now=NOW, lead_min=60)


def test_a_start_in_the_past_is_refused() -> None:
    with pytest.raises(BookingTooLate):
        validate_lead_time(NOW - timedelta(minutes=1), now=NOW, lead_min=0)


# --- the horizon ------------------------------------------------------------


def test_a_start_inside_the_horizon_is_allowed() -> None:
    validate_horizon(NOW + timedelta(days=30), now=NOW, horizon_days=60, zone=MOSCOW)


def test_a_start_beyond_the_horizon_is_refused() -> None:
    with pytest.raises(BookingTooFar) as failure:
        validate_horizon(NOW + timedelta(days=90), now=NOW, horizon_days=60, zone=MOSCOW)

    assert failure.value.code == "booking_too_far"


def test_the_last_date_of_the_horizon_is_bookable_to_its_end() -> None:
    # 60 days ahead, late in the salon's evening: still the last allowed date.
    late_on_the_last_day = datetime(2026, 12, 4, 19, 0, tzinfo=UTC)

    validate_horizon(late_on_the_last_day, now=NOW, horizon_days=60, zone=MOSCOW)


def test_the_day_after_the_horizon_is_refused() -> None:
    with pytest.raises(BookingTooFar):
        validate_horizon(
            datetime(2026, 12, 5, 7, 0, tzinfo=UTC), now=NOW, horizon_days=60, zone=MOSCOW
        )


# --- the grid ---------------------------------------------------------------


def test_a_start_on_the_grid_inside_working_time_is_allowed() -> None:
    validate_start_is_bookable(
        TEN_O_CLOCK + timedelta(minutes=30),
        work=WORKING,
        duration_min=45,
        buffer_min=0,
        step_min=15,
    )


def test_a_start_off_the_grid_is_refused() -> None:
    with pytest.raises(SlotOutsideSchedule) as failure:
        validate_start_is_bookable(
            TEN_O_CLOCK + timedelta(minutes=7),
            work=WORKING,
            duration_min=45,
            buffer_min=0,
            step_min=15,
        )

    assert failure.value.code == "slot_outside_schedule"


def test_a_start_outside_working_time_is_refused() -> None:
    with pytest.raises(SlotOutsideSchedule):
        validate_start_is_bookable(
            TEN_O_CLOCK - timedelta(hours=1),
            work=WORKING,
            duration_min=45,
            buffer_min=0,
            step_min=15,
        )


def test_a_service_that_does_not_fit_before_the_end_is_refused() -> None:
    with pytest.raises(SlotOutsideSchedule):
        validate_start_is_bookable(
            TEN_O_CLOCK + timedelta(hours=2, minutes=45),
            work=WORKING,
            duration_min=45,
            buffer_min=0,
            step_min=15,
        )


def test_a_day_without_working_time_refuses_every_start() -> None:
    with pytest.raises(SlotOutsideSchedule):
        validate_start_is_bookable(TEN_O_CLOCK, work=[], duration_min=45, buffer_min=0, step_min=15)


def test_the_buffer_does_not_have_to_fit_in_the_shift() -> None:
    # The last start of the shift, with cleaning up running past its end.
    validate_start_is_bookable(
        TEN_O_CLOCK + timedelta(hours=2, minutes=15),
        work=WORKING,
        duration_min=45,
        buffer_min=30,
        step_min=15,
    )
