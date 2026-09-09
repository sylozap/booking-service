"""What ``booking`` asks ``catalog`` before it lays out a day.

One call, one answer, everything needed. ``GET /internal/v1/masters/{id}/
services/{sid}`` returns whether the master is active, what the service costs
and how long it takes *for this master*, the snapshot of its name, and the four
booking policies of the salon together with its time zone. Availability is
computed from exactly this, and a booking is created from exactly this
(docs/04-api-contracts.md).

**One call rather than three.** Availability is the hot path of the platform,
and asking for the master, the service and the salon separately would put three
round trips and three failure modes in front of every request. It also makes
the answer internally consistent: three calls can observe a price changing
between them.

**The schema lives here, in the chassis, and is imported by both sides.**
``catalog`` builds it, ``booking`` parses it, and a field that changes shape
breaks the type check rather than the runtime (ADR-0005 applies the same
reasoning to events). This is the whole reason the contract is not simply a
dictionary at each end.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

__all__ = ["CATALOG_READ_SCOPE", "MasterServiceDetails", "SalonPolicies"]

# The scope a service token has to carry to read the catalog. Named here rather
# than spelled as a string on both sides: the producer of the endpoint and the
# configuration of the caller have to agree, and a typo in either is otherwise
# a 403 nobody can explain.
CATALOG_READ_SCOPE = "catalog:read"


class SalonPolicies(BaseModel):
    """The rules a salon books by, and the zone they are expressed in.

    Owned by ``catalog``, applied by ``booking`` (docs/02-domain-rules.md).
    They travel together because none of them means anything alone: a grid step
    without a time zone cannot be laid on a day, and a horizon in days is a
    number of local days.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    # An IANA identifier, never an offset. The schedule of a master is written
    # in this zone and unfolded into instants against it, so an offset would
    # move every appointment by an hour when the country changes its clocks.
    timezone: str

    slot_step_min: int
    booking_min_lead_min: int
    booking_horizon_days: int
    cancel_deadline_min: int


class MasterServiceDetails(BaseModel):
    """Everything ``booking`` needs about one master offering one service.

    ``extra="ignore"`` makes ``booking`` a tolerant reader: a field added to
    the answer by a newer ``catalog`` is skipped rather than fatal, so the two
    services can be deployed in either order.

    ``master_active`` is a flag and not an error. A deactivated master is a
    perfectly good answer to this question -- it is what lets ``booking``
    refuse the booking with ``master_inactive`` (docs/04-api-contracts.md)
    instead of turning a policy decision into a failed upstream call. The
    absence of the *link*, on the other hand, is a ``404``: there is nothing to
    describe.

    ``price`` is a ``Decimal`` and travels as a JSON string. Money as a JSON
    number goes through a float on the way in and comes back a hundredth short.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    master_id: UUID
    salon_id: UUID
    master_active: bool

    service_id: UUID
    # A snapshot: the booking copies this into its own row, so that renaming
    # the service later does not rewrite what a client was told they booked
    # (docs/05-data-model.md).
    service_name: str

    # Already resolved -- this master's override where there is one, the
    # salon's base figure otherwise. booking never sees the two halves and
    # never re-implements the choice between them.
    duration_min: int
    price: Decimal
    currency: str

    salon: SalonPolicies
