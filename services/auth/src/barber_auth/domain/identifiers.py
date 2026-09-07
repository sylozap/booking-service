"""Identifiers of the service, distinct at the type level.

Everything in this system is a UUID, and a signature that takes three of them
accepts them in any order. ``NewType`` costs nothing at runtime and makes mypy
refuse the transposition (docs/CODING_STANDARDS.md section 4).
"""

from __future__ import annotations

from typing import NewType
from uuid import UUID

__all__ = ["SalonId", "UserId"]

UserId = NewType("UserId", UUID)
# Owned by catalog. It appears here only as the scope of a role, and never as a
# foreign key: no constraint of this database may point at another service.
SalonId = NewType("SalonId", UUID)
