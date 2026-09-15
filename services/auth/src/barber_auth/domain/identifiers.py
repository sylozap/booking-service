"""Identifiers of the service, distinct at the type level.

``NewType`` wrappers over UUID, so mypy rejects identifiers passed in the wrong
order.
"""

from __future__ import annotations

from typing import NewType
from uuid import UUID

__all__ = ["SalonId", "UserId"]

UserId = NewType("UserId", UUID)
# Owned by catalog. It appears here only as the scope of a role, and never as a
# foreign key: no constraint of this database may point at another service.
SalonId = NewType("SalonId", UUID)
