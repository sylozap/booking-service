"""What a master actually charges, and how long they actually take.

The one rule the catalog has. A salon publishes a base price and a base
duration for each service; a master may override either, both, or neither, and
the figure that counts is ``COALESCE(override, base)``.

It lives here, as a value object over plain numbers, because it has two callers
that must not disagree: the master card of T2.3, which a visitor reads, and the
internal endpoint of T2.6, from which ``booking`` takes the duration it lays
out on the grid and the price it writes into the booking. A rule computed
separately in two scenarios is a rule that eventually shows one number and
charges another.

No imports beyond the standard library, on purpose: this module is the whole
domain of the service and has to stay copyable into an empty project together
with its tests (docs/CODING_STANDARDS.md section 2.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

__all__ = ["Offering"]


@dataclass(frozen=True, slots=True)
class Offering:
    """One service as one master offers it.

    Validated in the constructor rather than by whoever builds it: a value
    object that can exist in an impossible state pushes the check into every
    call site, and one of them will forget
    (docs/CODING_STANDARDS.md section 6).

    Money is ``Decimal``. A price expressed as a float is a price that is
    occasionally off by a hundredth, and the difference lands on a receipt.
    """

    base_price: Decimal
    base_duration_min: int
    price_override: Decimal | None = None
    duration_override: int | None = None

    def __post_init__(self) -> None:
        if self.base_duration_min <= 0:
            raise ValueError("base duration has to be positive")
        if self.base_price < 0:
            raise ValueError("base price cannot be negative")
        if self.duration_override is not None and self.duration_override <= 0:
            raise ValueError("a duration override has to be positive")
        if self.price_override is not None and self.price_override < 0:
            raise ValueError("a price override cannot be negative")

    @property
    def price(self) -> Decimal:
        """What the client pays.

        A zero override is a real price -- a service given away -- and not an
        absent one, which is why this tests against ``None`` rather than
        against falsehood.
        """
        return self.base_price if self.price_override is None else self.price_override

    @property
    def duration_min(self) -> int:
        """How long the master is occupied, before the buffer.

        The buffer after a booking belongs to ``booking``: it is a property of
        the master's working day rather than of the service, and it does not
        have to fit inside the shift (docs/02-domain-rules.md).
        """
        return self.base_duration_min if self.duration_override is None else self.duration_override
