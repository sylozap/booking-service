"""Which bookings a caller may see, and what they asked to narrow them to.

The scope is not a filter a caller chooses: it is who they are. Every query of
bookings takes one, and it is applied in SQL, so a page is never cut short by
rows thrown away after the fetch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from barber_booking.domain.booking_status import BookingStatus
from barber_booking.domain.errors import InvalidPeriod
from barber_booking.domain.identifiers import MasterId, SalonId, UserId

__all__ = ["BookingFilters", "BookingScope"]


@dataclass(frozen=True, slots=True)
class BookingScope:
    """Everything a caller may see: the union of what each of their roles opens.

    A client sees the bookings they made. A master also sees every booking
    with them, in the salons where they hold the role. A salon admin sees the
    whole of their salons, and a super admin everything.
    """

    user_id: UserId
    sees_everything: bool = False
    admin_of: frozenset[SalonId] = field(default_factory=frozenset)
    # Salons where the caller holds the master role; ``master_anywhere`` for a
    # grant that is not limited to one salon.
    master_in: frozenset[SalonId] = field(default_factory=frozenset)
    master_anywhere: bool = False

    @property
    def is_master(self) -> bool:
        return self.master_anywhere or bool(self.master_in)


@dataclass(frozen=True, slots=True)
class BookingFilters:
    """What the caller asked to narrow the listing to, inside their scope.

    ``starts_from`` is inclusive and ``starts_before`` exclusive, both on the
    start of the booking. An empty ``statuses`` means any status.
    """

    starts_from: datetime | None = None
    starts_before: datetime | None = None
    master_id: MasterId | None = None
    salon_id: SalonId | None = None
    statuses: frozenset[BookingStatus] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        for moment in (self.starts_from, self.starts_before):
            if moment is not None and moment.tzinfo is None:
                raise ValueError("a period is given in instants with a time zone")
        if (
            self.starts_from is not None
            and self.starts_before is not None
            and self.starts_before <= self.starts_from
        ):
            raise InvalidPeriod("The period has to end after it starts")
