"""The states a booking passes through, and which of them hold time."""

from __future__ import annotations

from enum import StrEnum

__all__ = ["ACTIVE_STATUSES", "CANCELLED_STATUSES", "OPEN_STATUSES", "BookingStatus"]


class BookingStatus(StrEnum):
    """Stored as text; the set is fixed by a CHECK in the database."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    COMPLETED = "completed"
    NO_SHOW = "no_show"
    CANCELLED_BY_CLIENT = "cancelled_by_client"
    CANCELLED_BY_SALON = "cancelled_by_salon"


# Statuses that keep the master occupied. A completed visit and a no-show still
# took the time; a cancellation frees it at once. The exclusion constraint
# spells the same list out, so adding a status is a migration, not only an edit
# here.
ACTIVE_STATUSES = frozenset(
    {
        BookingStatus.PENDING,
        BookingStatus.CONFIRMED,
        BookingStatus.COMPLETED,
        BookingStatus.NO_SHOW,
    }
)

# A visit still ahead: it can be moved, called off or, once it starts, closed.
OPEN_STATUSES = frozenset({BookingStatus.PENDING, BookingStatus.CONFIRMED})

CANCELLED_STATUSES = frozenset(
    {BookingStatus.CANCELLED_BY_CLIENT, BookingStatus.CANCELLED_BY_SALON}
)
