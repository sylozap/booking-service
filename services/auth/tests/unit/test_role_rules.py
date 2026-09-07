"""Who may hand out which role, with no database and no token in sight."""

from __future__ import annotations

from uuid import uuid4

import pytest

from barber_auth.domain.identifiers import SalonId
from barber_auth.domain.roles import Role, RoleGrant, may_grant

SALON = SalonId(uuid4())
ANOTHER_SALON = SalonId(uuid4())

SUPER_ADMIN = (RoleGrant(Role.SUPER_ADMIN),)
SALON_ADMIN = (RoleGrant(Role.SALON_ADMIN, SALON),)
MASTER = (RoleGrant(Role.MASTER, SALON),)
CLIENT = (RoleGrant(Role.CLIENT),)


@pytest.mark.parametrize(
    ("role", "salon_id"),
    [
        (Role.MASTER, SALON),
        (Role.MASTER, None),
        (Role.SALON_ADMIN, SALON),
        (Role.SUPER_ADMIN, None),
    ],
    ids=["master-in-salon", "master-everywhere", "salon-admin", "super-admin"],
)
def test_a_super_admin_may_grant_anything(role: Role, salon_id: SalonId | None) -> None:
    assert may_grant(held=SUPER_ADMIN, role=role, salon_id=salon_id) is True


def test_a_salon_admin_may_appoint_a_master_in_their_salon() -> None:
    assert may_grant(held=SALON_ADMIN, role=Role.MASTER, salon_id=SALON) is True


def test_a_salon_admin_may_not_reach_another_salon() -> None:
    # The check that keeps one salon's administrator out of another's staff.
    assert may_grant(held=SALON_ADMIN, role=Role.MASTER, salon_id=ANOTHER_SALON) is False


def test_a_salon_admin_may_not_appoint_a_master_everywhere() -> None:
    # A master with no salon is a master of every salon, which is not a thing
    # the administrator of one salon gets to decide.
    assert may_grant(held=SALON_ADMIN, role=Role.MASTER, salon_id=None) is False


def test_a_salon_admin_may_not_appoint_a_super_admin() -> None:
    assert may_grant(held=SALON_ADMIN, role=Role.SUPER_ADMIN, salon_id=None) is False


def test_a_salon_admin_may_not_multiply_themselves() -> None:
    # Not even in their own salon: handing over a salon is a decision for
    # whoever granted it, and without this rule the owner is the last to know.
    assert may_grant(held=SALON_ADMIN, role=Role.SALON_ADMIN, salon_id=SALON) is False


@pytest.mark.parametrize("held", [CLIENT, MASTER, ()], ids=["client", "master", "nothing"])
def test_an_ordinary_user_grants_nothing(held: tuple[RoleGrant, ...]) -> None:
    assert may_grant(held=held, role=Role.MASTER, salon_id=SALON) is False


@pytest.mark.parametrize("held", [SUPER_ADMIN, SALON_ADMIN], ids=["super-admin", "salon-admin"])
def test_the_client_role_is_never_grantable(held: tuple[RoleGrant, ...]) -> None:
    # It comes with registration. A second source for it is a second way for
    # the two to disagree.
    assert may_grant(held=held, role=Role.CLIENT, salon_id=None) is False


def test_a_global_salon_admin_reaches_every_salon() -> None:
    held = (RoleGrant(Role.SALON_ADMIN),)

    assert may_grant(held=held, role=Role.MASTER, salon_id=ANOTHER_SALON) is True


def test_holding_several_roles_takes_the_most_permissive() -> None:
    held = (RoleGrant(Role.CLIENT), RoleGrant(Role.SUPER_ADMIN))

    assert may_grant(held=held, role=Role.SALON_ADMIN, salon_id=SALON) is True
