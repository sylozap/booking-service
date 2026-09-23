"""Who may manage a master's schedule.

A ``super_admin`` anywhere, a ``salon_admin`` of the master's salon, and the
master themselves. Every refusal is the same ``403``, whatever its reason.
"""

from __future__ import annotations

from barber_booking.domain.master import MasterSettings
from barber_common.auth import Principal
from barber_common.errors import Forbidden
from barber_common.logging import get_logger

__all__ = ["SCHEDULE_MANAGERS", "require_schedule_access"]

_logger = get_logger(__name__)

# Who may reach a schedule endpoint at all. The salon and the account are
# checked by the scenario once the master is loaded.
SCHEDULE_MANAGERS = ("super_admin", "salon_admin", "master")


def require_schedule_access(caller: Principal, master: MasterSettings) -> None:
    """Refuse a caller who may not touch this master's schedule."""
    if caller.holds("super_admin") or caller.holds("salon_admin", salon_id=master.salon_id):
        return
    if caller.holds("master", salon_id=master.salon_id) and caller.user_id == master.user_id:
        return

    _logger.info("access refused", subject=caller.subject, salon_id=str(master.salon_id))
    raise Forbidden("This operation is not allowed for your role")
