"""The answer ``booking`` reads before it lays out a day.

One scenario, one query path, one response: the contract in
:mod:`barber_common.contracts.catalog`. Everything it reports comes from this
database, so the answer is internally consistent -- three separate calls could
observe a price changing between them.

The resolution of the final price and duration goes through
:class:`~barber_catalog.domain.pricing.Offering`, the same object the master
card uses. That is the point of having it: the figure a client is shown on the
card and the figure ``booking`` writes into the booking come from one
implementation, so they cannot drift.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from barber_catalog.domain.errors import MasterNotFound, MasterServiceNotFound
from barber_catalog.domain.identifiers import MasterId, SalonId, ServiceId
from barber_catalog.domain.pricing import Offering
from barber_catalog.repositories.master_services import MasterServiceRepository
from barber_catalog.repositories.masters import MasterRepository
from barber_catalog.repositories.salons import SalonRepository
from barber_common.contracts.catalog import MasterServiceDetails, SalonPolicies
from barber_common.db.session import transaction

__all__ = ["ReadMasterServiceDetails"]


class ReadMasterServiceDetails:
    """Describe one master offering one service, for another service."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._masters = MasterRepository(session)
        self._salons = SalonRepository(session)
        self._links = MasterServiceRepository(session)

    async def execute(
        self,
        *,
        master_id: MasterId,
        service_id: ServiceId,
    ) -> MasterServiceDetails:
        """Read everything at one consistent moment.

        The three reads share a transaction so that a price change landing
        between them cannot produce an answer where the duration comes from
        before it and the price from after. Read-only and short: nothing here
        makes a network call, so no connection is held across one
        (docs/CODING_STANDARDS.md section 7).

        **A deactivated master is reported, not refused.** ``master_active``
        false is a complete answer, and it is what lets ``booking`` say
        ``master_inactive`` rather than treat a policy decision as a failed
        upstream call. A missing *link* is a ``404``, because then there is
        nothing to describe -- and that is exactly the signal ``booking`` turns
        into ``service_not_offered``.
        """
        async with transaction(self._session):
            master = await self._masters.get(master_id)
            if master is None:
                raise MasterNotFound("No such master")

            link = await self._links.get_active_with_service(
                master_id=master_id, service_id=service_id
            )
            if link is None:
                raise MasterServiceNotFound("This master does not offer this service")

            # Guaranteed by the link: a service may only be taken up by a
            # master of its own salon (T2.5), so one salon covers both.
            salon = await self._salons.get(SalonId(master.salon_id))
            if salon is None:  # pragma: no cover - a master cannot outlive its salon
                raise MasterNotFound("No such master")

            offering = Offering(
                base_price=link.service.base_price,
                base_duration_min=link.service.base_duration_min,
                price_override=link.price_override,
                duration_override=link.duration_override,
            )

            return MasterServiceDetails(
                master_id=master.id,
                salon_id=salon.id,
                master_active=master.is_active,
                service_id=link.service_id,
                service_name=link.service.name,
                duration_min=offering.duration_min,
                price=offering.price,
                currency=link.service.currency,
                salon=SalonPolicies(
                    timezone=salon.timezone,
                    slot_step_min=salon.slot_step_min,
                    booking_min_lead_min=salon.booking_min_lead_min,
                    booking_horizon_days=salon.booking_horizon_days,
                    cancel_deadline_min=salon.cancel_deadline_min,
                ),
            )
