"""The card of a master: catalog and booking, asked at once, in one answer.

The one aggregating endpoint of the gateway (ADR-0009), and it stays the only
one: a second needs an ADR of its own.

Both services are asked at the same time, so the card costs the slower of the
two calls, not their sum. They do not weigh the same, though:

* without catalog there is no card -- a master catalog does not know is a
  ``404``, a catalog that does not answer a ``503``;
* without booking there is still a card -- the profile, with the slots marked
  ``unavailable`` rather than an empty list that would read as "fully booked".

The window of dates starts a day before today in UTC. The gateway does not know
the zone of the salon, and a salon east of Greenwich is already a day ahead;
booking drops the starts that have passed, so the extra day costs nothing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from barber_common.contracts.booking import AvailabilityResponse
from barber_common.contracts.catalog import MasterCardResponse
from barber_common.errors import DomainError
from barber_common.http import UpstreamError, UpstreamUnavailable
from barber_common.logging import get_logger
from barber_gateway.clients.booking import BookingClient
from barber_gateway.clients.catalog import CatalogClient
from barber_gateway.schemas.master_card import MasterCard, SlotsStatus

__all__ = ["ReadMasterCard"]

_logger = get_logger(__name__)

# The widest window booking answers is two weeks.
MAX_WINDOW_DAYS = 13


@dataclass(frozen=True, slots=True)
class _Slots:
    status: SlotsStatus
    availability: AvailabilityResponse | None = None


class ReadMasterCard:
    """Read the card of one master, with the nearest slots for one service."""

    def __init__(
        self,
        *,
        catalog: CatalogClient,
        booking: BookingClient,
        window_days: int,
        slots_limit: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not 0 < window_days <= MAX_WINDOW_DAYS:
            raise ValueError(f"window_days has to be between 1 and {MAX_WINDOW_DAYS}")
        self._catalog = catalog
        self._booking = booking
        self._window_days = window_days
        self._slots_limit = slots_limit
        self._clock = clock

    async def execute(self, *, master_id: UUID, service_id: UUID | None) -> MasterCard:
        try:
            async with asyncio.TaskGroup() as group:
                profile_task = group.create_task(self._catalog.get_master_card(master_id))
                slots_task = group.create_task(self._slots(master_id, service_id))
        except* DomainError as failure:
            # Without the profile there is no card, so its failure is the one
            # to report; otherwise it is the caller's mistake booking found.
            raise _failure_of(profile_task, failure) from None

        return _card(profile_task.result(), service_id, slots_task.result(), self._slots_limit)

    async def _slots(self, master_id: UUID, service_id: UUID | None) -> _Slots:
        if service_id is None:
            return _Slots(SlotsStatus.NOT_REQUESTED)

        date_from = self._clock().astimezone(UTC).date() - timedelta(days=1)
        try:
            availability = await self._booking.get_availability(
                master_id=master_id,
                service_id=service_id,
                date_from=date_from,
                date_to=date_from + timedelta(days=self._window_days),
            )
        except (UpstreamError, UpstreamUnavailable) as error:
            # WARNING: the card is still served, without the slots.
            _logger.warning(
                "master card served without slots", upstream="booking", error=error.code
            )
            return _Slots(SlotsStatus.UNAVAILABLE)
        return _Slots(SlotsStatus.OK, availability)


def _failure_of(
    profile_task: asyncio.Task[MasterCardResponse], failure: BaseExceptionGroup[DomainError]
) -> BaseException:
    if profile_task.done() and not profile_task.cancelled():
        error = profile_task.exception()
        if error is not None:
            return error
    return failure.exceptions[0]


def _card(
    profile: MasterCardResponse, service_id: UUID | None, slots: _Slots, limit: int
) -> MasterCard:
    availability = slots.availability
    starts = (
        [start for day in availability.days for start in day.slots][:limit]
        if availability is not None
        else []
    )
    return MasterCard(
        master=profile,
        service_id=service_id,
        timezone=availability.timezone if availability is not None else None,
        slots=starts,
        slots_status=slots.status,
    )
