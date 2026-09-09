"""Who may change a salon, and everything that belongs to one.

Two rules, and every write endpoint of the catalog is one of them.

**A ``super_admin`` may change anything.** **A ``salon_admin`` may change their
own salon**, meaning the salon their grant names -- its masters, its services
and the links between them.

The check cannot live in the dependency that guards the endpoint. A dependency
sees the request before anything is loaded, and which salon a master belongs to
is a fact in the database; ``require_roles`` therefore answers only "is this
caller an administrator of some kind", and the scenario asks the second half
here once it knows the salon (docs/CODING_STANDARDS.md section 9).

**One answer for two refusals.** "Your role does not allow this" and "that is
not your salon" are both ``403 forbidden_for_role`` with the same text. Telling
them apart would let an administrator of one salon discover which salons exist
by watching which identifiers answer differently
(docs/04-api-contracts.md).
"""

from __future__ import annotations

from uuid import UUID

from barber_common.auth import Principal
from barber_common.errors import Forbidden
from barber_common.logging import get_logger

__all__ = ["SALON_ADMINISTRATORS", "require_salon_scope"]

_logger = get_logger(__name__)

# Who may reach a write endpoint of the catalog at all. Passed to
# ``require_roles`` so that a caller holding neither is refused before a
# statement is issued.
SALON_ADMINISTRATORS = ("super_admin", "salon_admin")


def require_salon_scope(caller: Principal, salon_id: UUID) -> None:
    """Refuse a caller who may not administer this salon."""
    if caller.holds("super_admin") or caller.holds("salon_admin", salon_id=salon_id):
        return

    # INFO, not ERROR: a caller reaching for something they may not have is
    # ordinary traffic, and an alert on the error rate that fires on it is an
    # alert nobody reads (docs/CODING_STANDARDS.md section 11). The salon is
    # low cardinality and safe to log; the token never is.
    _logger.info("access refused", subject=caller.subject, salon_id=str(salon_id))
    raise Forbidden("This operation is not allowed for your role")
