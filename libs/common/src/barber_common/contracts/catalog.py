"""Contract of the internal catalog endpoint used by ``booking``.

``GET /internal/v1/masters/{id}/services/{sid}`` returns in one call whether the
master is active, the price and duration for this master, a snapshot of the
service name, and the salon time zone with its booking policies. The schema is
imported by both ``catalog`` and ``booking``.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

__all__ = ["CATALOG_READ_SCOPE", "MasterServiceDetails", "SalonPolicies"]

# The scope a service token has to carry to read the catalog, shared by the
# endpoint and its callers.
CATALOG_READ_SCOPE = "catalog:read"


class SalonPolicies(BaseModel):
    """The booking policies of a salon and the time zone they apply in."""

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

    Unknown fields are ignored, so the services can be deployed in either
    order. A deactivated master is reported through ``master_active`` rather
    than as an error. ``price`` is a ``Decimal`` serialised as a JSON string.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    master_id: UUID
    salon_id: UUID
    master_active: bool

    service_id: UUID
    # A snapshot: the booking copies it into its own row, so renaming the
    # service later does not change past bookings.
    service_name: str

    # Already resolved -- this master's override where there is one, the
    # salon's base figure otherwise. booking never sees the two halves and
    # never re-implements the choice between them.
    duration_min: int
    price: Decimal
    currency: str

    salon: SalonPolicies
