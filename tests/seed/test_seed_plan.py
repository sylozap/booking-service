"""What the seed decides before it asks the platform anything."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import seed
from seed import Candidate, build_plan, choose_bookings


def test_plan_is_three_salons_twelve_masters_fifteen_services_and_a_hundred_clients() -> None:
    plan = build_plan()

    services = [service for menu in plan.services.values() for service in menu]

    assert len(plan.salons) == 3
    assert len(plan.masters) == 12
    assert len(services) == 15
    assert len(plan.clients) == 100


def test_salons_are_in_three_time_zones() -> None:
    plan = build_plan()

    assert len({salon.timezone for salon in plan.salons}) == 3


def test_every_salon_has_four_masters() -> None:
    plan = build_plan()

    per_salon = {salon.name: 0 for salon in plan.salons}
    for master in plan.masters:
        per_salon[master.salon] += 1

    assert set(per_salon.values()) == {4}


def test_a_master_offers_only_services_of_their_own_salon() -> None:
    plan = build_plan()

    for master in plan.masters:
        menu = {service.name for service in plan.services[master.salon]}
        assert {offer.service for offer in master.offers} <= menu
        assert len(master.offers) >= 3


def test_accounts_never_share_an_email_or_a_phone() -> None:
    plan = build_plan()

    accounts = [(m.email, m.phone) for m in plan.masters] + [
        (c.email, c.phone) for c in plan.clients
    ]

    assert len({email for email, _ in accounts}) == len(accounts)
    assert len({phone for _, phone in accounts}) == len(accounts)


def test_days_off_are_never_today_or_tomorrow() -> None:
    # Today in UTC is tomorrow in Novosibirsk for part of the day; a day off
    # there must still be in the future.
    plan = build_plan()

    assert all(min(master.days_off_in) >= 2 for master in plan.masters)


def test_plan_is_the_same_every_time() -> None:
    assert build_plan() == build_plan()


def test_plan_follows_the_random_seed() -> None:
    assert build_plan(random.Random(1)) != build_plan(random.Random(2))


# --- choosing what to book --------------------------------------------------------

START = datetime(2026, 10, 5, 7, 0, tzinfo=UTC)


def every_quarter(hours: int, duration_min: int) -> list[Candidate]:
    """Starts every fifteen minutes, as availability lists them."""
    service_id = uuid4()
    return [
        Candidate(service_id, START + timedelta(minutes=15 * step), duration_min)
        for step in range(hours * 4)
    ]


def test_chosen_bookings_of_a_master_never_overlap() -> None:
    candidates = every_quarter(hours=9, duration_min=45) + every_quarter(hours=9, duration_min=75)

    chosen = choose_bookings(candidates, 12, random.Random(7))

    for earlier, later in zip(chosen, chosen[1:], strict=False):
        assert earlier.end <= later.start


def test_no_more_bookings_than_asked_for() -> None:
    chosen = choose_bookings(every_quarter(hours=9, duration_min=30), 5, random.Random(7))

    assert len(chosen) == 5


def test_a_full_day_gives_what_fits_and_no_more() -> None:
    # Two hours of 45-minute visits: two fit, whichever starts are drawn,
    # and at most two.
    chosen = choose_bookings(every_quarter(hours=2, duration_min=45), 10, random.Random(7))

    assert 1 <= len(chosen) <= 2


def test_nothing_is_chosen_from_nothing() -> None:
    assert choose_bookings([], 10, random.Random(7)) == []


def test_bookings_cover_two_weeks() -> None:
    assert seed.BOOKING_DAYS == 14
