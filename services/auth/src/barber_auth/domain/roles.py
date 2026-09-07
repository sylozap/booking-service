"""Roles a user can hold, and who may hand them out.

A role is either global or scoped to one salon. ``client``, ``master`` and
``super_admin`` are global; ``salon_admin`` is meaningless without the salon it
administers, and :meth:`Role.is_scoped_to_salon` is what says so in one place
instead of in every scenario that assigns a role.

:func:`may_grant` is the authorisation rule of T1.9, written as a pure function
over what the caller holds. It takes domain grants rather than a verified token
because the domain does not depend on the chassis
(docs/CODING_STANDARDS.md section 2.2); translating one into the other is the
job of the scenario, which is allowed to know about both.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from barber_auth.domain.identifiers import SalonId

__all__ = ["GRANTABLE_ROLES", "Role", "RoleGrant", "may_grant"]


class Role(StrEnum):
    """The four roles of docs/04-api-contracts.md."""

    CLIENT = "client"
    MASTER = "master"
    SALON_ADMIN = "salon_admin"
    SUPER_ADMIN = "super_admin"

    @property
    def is_scoped_to_salon(self) -> bool:
        """Whether the role only makes sense together with a salon."""
        return self is Role.SALON_ADMIN


# ``client`` is deliberately absent. It comes with registration, and a second
# way to hand it out is a second way for the two to disagree -- an account
# with no client role, or one that has it twice over.
GRANTABLE_ROLES = frozenset({Role.MASTER, Role.SALON_ADMIN, Role.SUPER_ADMIN})


@dataclass(frozen=True, slots=True)
class RoleGrant:
    """One role a user holds, and the salon it is limited to, if any.

    ``salon_id`` is ``None`` for the global roles. The same shape ends up
    inside the ``roles`` claim of an access token, so a service reading the
    token learns not just that someone is a ``salon_admin`` but of which salon
    (docs/04-api-contracts.md).
    """

    role: Role
    salon_id: SalonId | None = None

    @property
    def is_global(self) -> bool:
        """Whether the grant applies everywhere rather than in one salon."""
        return self.salon_id is None

    def as_claim(self) -> dict[str, str | None]:
        """The member of the ``roles`` array that stands for this grant."""
        return {
            "role": self.role.value,
            "salon_id": str(self.salon_id) if self.salon_id is not None else None,
        }


def may_grant(
    *,
    held: tuple[RoleGrant, ...],
    role: Role,
    salon_id: SalonId | None,
) -> bool:
    """Whether a caller holding ``held`` may grant ``role`` in ``salon_id``.

    Two rules and no more.

    A ``super_admin`` grants anything, anywhere. A ``salon_admin`` grants
    ``master``, and only inside a salon they administer -- which is the check
    that keeps one salon's administrator out of another's staff list.

    Notably a ``salon_admin`` cannot appoint another ``salon_admin``, not even
    in their own salon. Handing over a salon is a decision for the person who
    granted the salon in the first place; without that rule an administrator
    multiplies themselves and the owner is the last to know.
    """
    if role not in GRANTABLE_ROLES:
        return False

    if any(grant.role is Role.SUPER_ADMIN for grant in held):
        return True

    if role is not Role.MASTER or salon_id is None:
        return False

    return any(
        grant.role is Role.SALON_ADMIN and (grant.is_global or grant.salon_id == salon_id)
        for grant in held
    )
