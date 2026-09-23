"""Cutting working time into the starts a client may book."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from barber_booking.domain.slots import slot_starts
from barber_booking.domain.time_range import TimeRange


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 5, hour, minute, tzinfo=UTC)


def span(start: datetime, end: datetime) -> TimeRange:
    return TimeRange(start, end)


def test_a_slot_may_end_exactly_where_the_interval_ends() -> None:
    starts = slot_starts(
        work=[span(at(10), at(11))], busy=[], duration_min=45, buffer_min=0, step_min=15
    )

    assert starts == [at(10), at(10, 15)]


def test_a_slot_that_does_not_fit_before_the_end_is_not_offered() -> None:
    starts = slot_starts(
        work=[span(at(10), at(11))], busy=[], duration_min=50, buffer_min=0, step_min=15
    )

    assert starts == [at(10)]


def test_the_buffer_may_run_past_the_end_of_the_shift() -> None:
    starts = slot_starts(
        work=[span(at(10), at(11))], busy=[], duration_min=60, buffer_min=15, step_min=15
    )

    assert starts == [at(10)]


def test_a_step_of_15_with_a_service_of_45() -> None:
    starts = slot_starts(
        work=[span(at(10), at(12))], busy=[], duration_min=45, buffer_min=0, step_min=15
    )

    assert starts == [at(10), at(10, 15), at(10, 30), at(10, 45), at(11), at(11, 15)]


def test_the_buffer_of_a_booking_closes_the_next_start() -> None:
    # 10:00-10:45 with a buffer of 15 occupies the master until 11:00.
    booked = span(at(10), at(11))

    starts = slot_starts(
        work=[span(at(10), at(12))], busy=[booked], duration_min=45, buffer_min=15, step_min=15
    )

    assert at(10, 45) not in starts
    assert at(11) in starts


def test_the_buffer_of_the_new_booking_may_not_reach_the_next_one() -> None:
    booked = span(at(11), at(12))

    starts = slot_starts(
        work=[span(at(10), at(12))], busy=[booked], duration_min=45, buffer_min=15, step_min=15
    )

    assert starts == [at(10)]


def test_the_grid_starts_again_at_each_interval() -> None:
    starts = slot_starts(
        work=[span(at(10), at(11)), span(at(13, 10), at(14))],
        busy=[],
        duration_min=45,
        buffer_min=0,
        step_min=15,
    )

    assert starts == [at(10), at(10, 15), at(13, 10)]


def test_a_service_of_zero_minutes_is_an_error() -> None:
    with pytest.raises(ValueError, match="duration"):
        slot_starts(work=[span(at(10), at(11))], busy=[], duration_min=0, buffer_min=0, step_min=15)


def test_a_step_of_zero_minutes_is_an_error() -> None:
    with pytest.raises(ValueError, match="step"):
        slot_starts(work=[span(at(10), at(11))], busy=[], duration_min=45, buffer_min=0, step_min=0)


def test_a_negative_buffer_is_an_error() -> None:
    with pytest.raises(ValueError, match="buffer"):
        slot_starts(
            work=[span(at(10), at(11))], busy=[], duration_min=45, buffer_min=-5, step_min=15
        )


def test_a_range_must_end_after_it_starts() -> None:
    with pytest.raises(ValueError, match="end after it starts"):
        TimeRange(at(11), at(10))


def test_a_range_must_be_aware_of_its_zone() -> None:
    with pytest.raises(ValueError, match="time zone"):
        TimeRange(datetime(2026, 10, 5, 10), datetime(2026, 10, 5, 11))  # noqa: DTZ001
