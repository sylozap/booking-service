"""Who may manage a master's schedule, and in what capacity a caller acts on a booking.

A schedule: a ``super_admin`` anywhere, a ``salon_admin`` of the master's
salon, and the master themselves. Every refusal is the same ``403``, whatever
its reason.

A booking: the token says which roles the caller holds, and the booking says
whose it is. What each capacity may do is the domain's rule, not this module's.
"""

from __future__ import annotations

from barber_booking.domain.booking import Actor, Booking
from barber_booking.domain.identifiers import UserId
from barber_booking.domain.master import MasterSettings
from barber_common.auth import Principal
from barber_common.errors import Forbidden
from barber_common.logging import get_logger

__all__ = ["BOOKING_ROLES", "SCHEDULE_MANAGERS", "booking_actor", "require_schedule_access"]

_logger = get_logger(__name__)

# Who may reach a schedule endpoint at all. The salon and the account are
# checked by the scenario once the master is loaded.
SCHEDULE_MANAGERS = ("super_admin", "salon_admin", "master")

# Who may reach a booking endpoint at all. Every registered user is a client,
# so this narrows nothing for people; it closes the endpoints to a service.
BOOKING_ROLES = ("client", "master", "salon_admin", "super_admin")


def require_schedule_access(caller: Principal, master: MasterSettings) -> None:
    """Refuse a caller who may not touch this master's schedule."""
    if caller.holds("super_admin") or caller.holds("salon_admin", salon_id=master.salon_id):
        return
    if caller.holds("master", salon_id=master.salon_id) and caller.user_id == master.user_id:
        return

    _logger.info("access refused", subject=caller.subject, salon_id=str(master.salon_id))
    raise Forbidden("This operation is not allowed for your role")


def booking_actor(caller: Principal, booking: Booking, master: MasterSettings | None) -> Actor:
    """The capacities in which the caller stands towards this booking.

    ``master`` is the settings row of the booking's master, which carries the
    account behind them; without it nobody is recognised as the master.
    """
    user_id = UserId(caller.user_id)
    return Actor(
        user_id=user_id,
        is_client=user_id == booking.client_user_id,
        runs_salon=caller.holds("super_admin")
        or caller.holds("salon_admin", salon_id=booking.salon_id),
        is_master=master is not None
        and master.user_id == user_id
        and caller.holds("master", salon_id=booking.salon_id),
    )
