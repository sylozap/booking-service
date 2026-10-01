"""DTOs of the synchronous calls between services.

Imported by both the caller and the callee, so a mismatch is caught by mypy.
Domain types stay in the service that owns them.
"""

from barber_common.contracts.booking import AvailabilityResponse, DayAvailability
from barber_common.contracts.catalog import (
    CATALOG_READ_SCOPE,
    MasterCardResponse,
    MasterProfile,
    MasterResponse,
    MasterServiceDetails,
    OfferedServiceResponse,
    SalonPolicies,
)

__all__ = [
    "CATALOG_READ_SCOPE",
    "AvailabilityResponse",
    "DayAvailability",
    "MasterCardResponse",
    "MasterProfile",
    "MasterResponse",
    "MasterServiceDetails",
    "OfferedServiceResponse",
    "SalonPolicies",
]
