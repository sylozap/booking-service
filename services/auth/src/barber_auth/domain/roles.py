"""Roles a user can hold, and who may grant them.

A grant is either global or scoped to one salon; ``salon_admin`` always needs a
salon, as :meth:`Role.is_scoped_to_salon` says. :func:`may_grant` is the
authorisation rule for granting roles, as a pure function over the grants the
caller holds.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from barber_auth.domain.identifiers import SalonId

__all__ = ["GRANTABLE_ROLES", "Role", "RoleGrant", "may_grant"]


class Role(StrEnum):
    """The four roles of the platform."""

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

    ``salon_id`` is ``None`` for global roles. The same shape is used in the
    ``roles`` claim of an access token.
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

    A ``super_admin`` grants any role anywhere. A ``salon_admin`` grants only
    ``master``, and only in a salon they administer.
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
