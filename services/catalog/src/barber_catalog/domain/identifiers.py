"""Identifiers of the service, distinct at the type level.

``NewType`` wrappers over UUID, so mypy rejects identifiers passed in the wrong
order.
"""

from __future__ import annotations

from typing import NewType
from uuid import UUID

__all__ = ["MasterId", "SalonId", "ServiceId", "UserId"]

SalonId = NewType("SalonId", UUID)
MasterId = NewType("MasterId", UUID)
# A service in the domain sense, such as a haircut, not a platform service.
ServiceId = NewType("ServiceId", UUID)
# An account owned by auth, referenced without a foreign key.
UserId = NewType("UserId", UUID)
