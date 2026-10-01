"""The routing table: which service answers which path."""

from __future__ import annotations

import pytest

from barber_gateway.routing import PathPattern, Upstream, route_for

MASTER = "0192f3c1-6a2b-7c3d-8e4f-5a6b7c8d9e0f"
SERVICE = "0192f3c1-0000-7c3d-8e4f-5a6b7c8d9e0f"


@pytest.mark.parametrize(
    ("path", "upstream"),
    [
        ("/api/v1/auth/login", Upstream.AUTH),
        ("/api/v1/users/42/roles", Upstream.AUTH),
        ("/.well-known/jwks.json", Upstream.AUTH),
        ("/api/v1/salons", Upstream.CATALOG),
        ("/api/v1/salons/1/masters", Upstream.CATALOG),
        ("/api/v1/services/1/archive", Upstream.CATALOG),
        ("/api/v1/masters", Upstream.CATALOG),
        (f"/api/v1/masters/{MASTER}", Upstream.CATALOG),
        (f"/api/v1/masters/{MASTER}/deactivate", Upstream.CATALOG),
        (f"/api/v1/masters/{MASTER}/services/{SERVICE}", Upstream.CATALOG),
        (f"/api/v1/masters/{MASTER}/schedule", Upstream.BOOKING),
        (f"/api/v1/masters/{MASTER}/schedule/exceptions/1", Upstream.BOOKING),
        (f"/api/v1/masters/{MASTER}/settings", Upstream.BOOKING),
        ("/api/v1/availability", Upstream.BOOKING),
        ("/api/v1/bookings", Upstream.BOOKING),
        ("/api/v1/bookings/1/no-show", Upstream.BOOKING),
        ("/api/v1/notifications/preferences", Upstream.NOTIFICATION),
    ],
)
def test_a_path_goes_to_the_service_that_owns_it(path: str, upstream: Upstream) -> None:
    assert route_for(path) is upstream


@pytest.mark.parametrize(
    "path",
    [
        "/internal/v1/token",
        "/internal/v1/masters/1",
        "/api/v1/bookingsx",
        "/api/v2/bookings",
        "/api/v1",
        "/api/v1/unknown",
    ],
)
def test_a_path_no_service_owns_goes_nowhere(path: str) -> None:
    assert route_for(path) is None


def test_the_schedule_of_a_master_is_not_taken_for_a_master_called_schedule() -> None:
    # The placeholder stands for one segment: /masters/schedule is a master id
    # of catalog's, not booking's schedule of nobody.
    assert route_for("/api/v1/masters/schedule") is Upstream.CATALOG


def test_an_exact_pattern_refuses_a_longer_path() -> None:
    pattern = PathPattern("/api/v1/masters/{master_id}", exact=True)

    assert pattern.matches(f"/api/v1/masters/{MASTER}")
    assert not pattern.matches(f"/api/v1/masters/{MASTER}/schedule")
    assert not pattern.matches("/api/v1/masters")
