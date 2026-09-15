"""The pricing rule of the catalog, tested without a database."""

from __future__ import annotations

from decimal import Decimal

import pytest

from barber_catalog.domain.pricing import Offering

BASE_PRICE = Decimal("3500.00")
BASE_DURATION = 45


@pytest.mark.parametrize(
    ("price_override", "duration_override", "price", "duration_min"),
    [
        (None, None, BASE_PRICE, BASE_DURATION),
        (Decimal("4200.00"), None, Decimal("4200.00"), BASE_DURATION),
        (None, 60, BASE_PRICE, 60),
        (Decimal("4200.00"), 60, Decimal("4200.00"), 60),
    ],
    ids=["neither", "price only", "duration only", "both"],
)
def test_an_override_wins_and_an_absent_one_falls_back(
    price_override: Decimal | None,
    duration_override: int | None,
    price: Decimal,
    duration_min: int,
) -> None:
    offering = Offering(
        base_price=BASE_PRICE,
        base_duration_min=BASE_DURATION,
        price_override=price_override,
        duration_override=duration_override,
    )

    assert offering.price == price
    assert offering.duration_min == duration_min


def test_a_free_service_is_a_price_and_not_an_absent_override() -> None:
    """Zero is falsy and is nevertheless a real price.

    Testing the override for truth rather than for ``None`` would quietly
    charge the base price for a service the master gives away.
    """
    offering = Offering(
        base_price=BASE_PRICE,
        base_duration_min=BASE_DURATION,
        price_override=Decimal("0.00"),
    )

    assert offering.price == Decimal("0.00")


def test_the_price_keeps_its_scale() -> None:
    """Decimal and never float: a hundredth lost here lands on a receipt."""
    offering = Offering(base_price=Decimal("0.10"), base_duration_min=BASE_DURATION)

    assert str(offering.price * 3) == "0.30"


def test_a_service_of_no_duration_cannot_exist() -> None:
    with pytest.raises(ValueError, match="base duration"):
        Offering(base_price=BASE_PRICE, base_duration_min=0)


def test_a_negative_base_price_cannot_exist() -> None:
    with pytest.raises(ValueError, match="base price"):
        Offering(base_price=Decimal("-1.00"), base_duration_min=BASE_DURATION)


def test_a_duration_override_of_zero_cannot_exist() -> None:
    with pytest.raises(ValueError, match="duration override"):
        Offering(
            base_price=BASE_PRICE,
            base_duration_min=BASE_DURATION,
            duration_override=0,
        )


def test_a_negative_price_override_cannot_exist() -> None:
    with pytest.raises(ValueError, match="price override"):
        Offering(
            base_price=BASE_PRICE,
            base_duration_min=BASE_DURATION,
            price_override=Decimal("-1.00"),
        )
