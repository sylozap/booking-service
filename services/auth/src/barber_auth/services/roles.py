"""Granting and revoking roles.

A ``super_admin`` grants any role. A ``salon_admin`` grants ``master``, and only
in a salon they administer. ``client`` is never granted here. A change reaches
the user's access token on their next refresh, and no event is published.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from barber_auth.domain.errors import (
    RoleNotGrantable,
    RoleNotHeld,
    UserNotFound,
)
from barber_auth.domain.identifiers import SalonId, UserId
from barber_auth.domain.roles import GRANTABLE_ROLES, Role, RoleGrant, may_grant
from barber_auth.repositories.users import UserRepository
from barber_common.auth import Principal
from barber_common.db.errors import SQLSTATE_UNIQUE_VIOLATION, sqlstate_of
from barber_common.db.session import transaction
from barber_common.errors import Forbidden
from barber_common.logging import get_logger

__all__ = ["GrantRole", "GrantedRole", "RevokeRole"]

_logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class GrantedRole:
    """One grant, as the caller sees it afterwards."""

    user_id: UserId
    role: Role
    salon_id: SalonId | None


class GrantRole:
    """Give a user a role, if the caller is allowed to hand it out."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._users = UserRepository(session)

    async def execute(
        self,
        *,
        caller: Principal,
        user_id: UserId,
        role: Role,
        salon_id: SalonId | None = None,
    ) -> GrantedRole:
        """Grant one role to one user."""
        # Grantability first, so even a super_admin asking for ``client`` gets
        # 422 rather than 403.
        _reject_ungrantable(role)
        _authorize(caller=caller, role=role, salon_id=salon_id)

        if role.is_scoped_to_salon and salon_id is None:
            raise RoleNotGrantable(f"Role {role.value} has to name the salon it applies to")

        async with transaction(self._session):
            if await self._users.get_by_id(user_id) is None:
                raise UserNotFound("No such user")

            try:
                await self._users.grant_role(user_id=user_id, role=role, salon_id=salon_id)
            except IntegrityError as error:
                if sqlstate_of(error) != SQLSTATE_UNIQUE_VIOLATION:
                    raise
                # The role is already held: the requested state exists, so this
                # is not a conflict.
                await self._session.rollback()
                return GrantedRole(user_id=user_id, role=role, salon_id=salon_id)

        _logger.info(
            "role granted",
            user_id=str(user_id),
            role=role.value,
            salon_id=str(salon_id) if salon_id else None,
            granted_by=caller.subject,
        )
        return GrantedRole(user_id=user_id, role=role, salon_id=salon_id)


class RevokeRole:
    """Take a role away, under the same rules that allow granting it."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._users = UserRepository(session)

    async def execute(
        self,
        *,
        caller: Principal,
        user_id: UserId,
        role: Role,
        salon_id: SalonId | None = None,
    ) -> None:
        """Revoke one grant.

        Allowed to whoever may grant the role. ``client`` cannot be revoked.
        """
        _reject_ungrantable(role)
        _authorize(caller=caller, role=role, salon_id=salon_id)

        async with transaction(self._session):
            if await self._users.get_by_id(user_id) is None:
                raise UserNotFound("No such user")

            removed = await self._users.revoke_role(user_id=user_id, role=role, salon_id=salon_id)

        if not removed:
            # Named separately from "no such user": the caller is allowed to
            # see this user, so telling them the grant is absent reveals
            # nothing and saves them guessing why nothing changed.
            raise RoleNotHeld("This user does not hold that role")

        _logger.info(
            "role revoked",
            user_id=str(user_id),
            role=role.value,
            salon_id=str(salon_id) if salon_id else None,
            revoked_by=caller.subject,
        )


def _reject_ungrantable(role: Role) -> None:
    """Refuse a role that is not handed out through this API at all."""
    if role not in GRANTABLE_ROLES:
        raise RoleNotGrantable(
            f"Role {role.value} is not granted through this endpoint: it comes with registration"
        )


def _held_by(caller: Principal) -> tuple[RoleGrant, ...]:
    """The roles of a verified token, as the domain understands them.

    The translation between a chassis type and a domain one. A role the token
    carries that this service does not know is dropped: the token verified, so
    the platform issued it, and an unknown role grants nothing here anyway.
    """
    grants = []
    for claim in caller.roles:
        try:
            role = Role(claim.role)
        except ValueError:
            continue
        grants.append(
            RoleGrant(role=role, salon_id=SalonId(claim.salon_id) if claim.salon_id else None)
        )
    return tuple(grants)


def _authorize(*, caller: Principal, role: Role, salon_id: SalonId | None) -> None:
    """Refuse a grant the caller is not entitled to make.

    One answer for "your role does not allow this" and "not in that salon".
    A separate answer for the second would let a salon administrator map which
    salons exist by watching which ones answer differently.
    """
    if not may_grant(held=_held_by(caller), role=role, salon_id=salon_id):
        _logger.info(
            "role change refused",
            subject=caller.subject,
            role=role.value,
        )
        raise Forbidden("This operation is not allowed for your role")
