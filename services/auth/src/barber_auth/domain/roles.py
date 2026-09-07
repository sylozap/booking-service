"""Roles a user can hold.

A role is either global or scoped to one salon. ``client``, ``master`` and
``super_admin`` are global; ``salon_admin`` is meaningless without the salon it
administers, and :meth:`Role.is_scoped_to_salon` is what says so in one place
instead of in every scenario that assigns a role.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["Role"]


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
