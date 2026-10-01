"""The arithmetic of the sliding window counter, without Redis."""

from __future__ import annotations

import pytest
from starlette.requests import Request

from barber_gateway.rate_limit import (
    Limit,
    LimitScope,
    RateLimitPolicy,
    client_address,
    retry_after_seconds,
    sliding_estimate,
)

MINUTE_MS = 60_000


def test_the_previous_window_counts_by_the_share_still_inside_the_sliding_one() -> None:
    estimate = sliding_estimate(previous=30, current=6, elapsed_ms=45_000, window_ms=MINUTE_MS)

    assert estimate == pytest.approx(30 * 0.25 + 6)


def test_a_full_previous_window_still_counts_whole_right_after_the_boundary() -> None:
    estimate = sliding_estimate(previous=30, current=0, elapsed_ms=0, window_ms=MINUTE_MS)

    assert estimate == 30


def test_the_wait_is_until_enough_of_the_previous_window_slides_out() -> None:
    # 30 of 30 a moment ago: one fits once a thirtieth of them has slid out,
    # two seconds into the new window.
    wait = retry_after_seconds(limit=30, previous=30, current=0, elapsed_ms=0, window_ms=MINUTE_MS)

    assert wait == 2


def test_a_full_current_window_makes_the_wait_run_into_the_next_one() -> None:
    wait = retry_after_seconds(
        limit=5, previous=0, current=5, elapsed_ms=50_000, window_ms=MINUTE_MS
    )

    # Ten seconds to the boundary, then twelve more for a fifth of five to go.
    assert wait == 22


def test_the_wait_is_never_shorter_than_a_second() -> None:
    wait = retry_after_seconds(
        limit=30, previous=30, current=0, elapsed_ms=59_999, window_ms=MINUTE_MS
    )

    assert wait == 1


def test_a_limit_admits_at_least_one_request() -> None:
    with pytest.raises(ValueError, match="at least one"):
        Limit(LimitScope.USER, 0, 60)


POLICY = RateLimitPolicy(
    anonymous=Limit(LimitScope.ANONYMOUS, 30, 60),
    user=Limit(LimitScope.USER, 120, 60),
    booking_create=Limit(LimitScope.BOOKING_CREATE, 5, 60),
    registration=Limit(LimitScope.REGISTRATION, 3, 3600),
)


def test_a_stranger_is_counted_by_address_and_a_user_by_account() -> None:
    stranger = POLICY.limits_for(method="GET", path="/api/v1/salons", user_id=None, client_ip="1")
    user = POLICY.limits_for(method="GET", path="/api/v1/salons", user_id="u", client_ip="1")

    assert stranger == [(POLICY.anonymous, "1")]
    assert user == [(POLICY.user, "u")]


def test_creating_a_booking_counts_against_the_strict_limit_too() -> None:
    limits = POLICY.limits_for(method="POST", path="/api/v1/bookings", user_id="u", client_ip="1")

    assert limits == [(POLICY.user, "u"), (POLICY.booking_create, "u")]


def test_registering_counts_against_the_hourly_limit_of_the_address() -> None:
    limits = POLICY.limits_for(
        method="POST", path="/api/v1/auth/register", user_id=None, client_ip="1"
    )

    assert limits == [(POLICY.anonymous, "1"), (POLICY.registration, "1")]


def test_reading_bookings_is_not_creating_one() -> None:
    limits = POLICY.limits_for(method="GET", path="/api/v1/bookings", user_id="u", client_ip="1")

    assert limits == [(POLICY.user, "u")]


def _request(peer: str, forwarded_for: str | None) -> Request:
    headers = [] if forwarded_for is None else [(b"x-forwarded-for", forwarded_for.encode())]
    return Request({"type": "http", "headers": headers, "client": (peer, 1234)})


def test_without_a_trusted_proxy_the_forwarded_header_is_not_believed() -> None:
    address = client_address(_request("10.0.0.7", "6.6.6.6"), trusted_proxy_hops=0)

    assert address == "10.0.0.7"


def test_behind_one_trusted_proxy_the_address_it_saw_is_the_clients() -> None:
    # The client forged the first entry; the Ingress appended the real one.
    address = client_address(_request("10.0.0.2", "6.6.6.6, 203.0.113.9"), trusted_proxy_hops=1)

    assert address == "203.0.113.9"


def test_more_trusted_hops_than_entries_falls_back_to_the_first_one() -> None:
    address = client_address(_request("10.0.0.2", "203.0.113.9"), trusted_proxy_hops=5)

    assert address == "203.0.113.9"
