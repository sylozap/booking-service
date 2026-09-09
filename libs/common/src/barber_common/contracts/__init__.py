"""DTOs of the synchronous calls between services.

The counterpart of :mod:`barber_common.events`: that one carries the schemas of
asynchronous messages, this one the bodies of HTTP calls one service makes to
another. Both live in the chassis for the same reason -- the two sides drifting
apart has to be caught by mypy, not by a 500 in production
(docs/CODING_STANDARDS.md section 2.3).

What does **not** belong here is a domain type. A contract describes what
crosses a boundary; the rules behind it stay in the service that owns them
(ADR-0016).
"""

from barber_common.contracts.catalog import (
    CATALOG_READ_SCOPE,
    MasterServiceDetails,
    SalonPolicies,
)

__all__ = [
    "CATALOG_READ_SCOPE",
    "MasterServiceDetails",
    "SalonPolicies",
]
