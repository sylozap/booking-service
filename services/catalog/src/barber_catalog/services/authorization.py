"""Who may change a salon and everything that belongs to it.

A ``super_admin`` may change anything; a ``salon_admin`` only their own salon,
its masters, services and links. Checked by the scenario once the salon is
known. Both refusals answer ``403 forbidden_for_role`` with the same text.
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

    # INFO, not ERROR: a refused caller is ordinary traffic.
    _logger.info("access refused", subject=caller.subject, salon_id=str(salon_id))
    raise Forbidden("This operation is not allowed for your role")
