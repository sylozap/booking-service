"""Identifiers of the service, distinct at the type level.

Everything in this system is a UUID, and a signature taking a master, a service
and a salon accepts them in any order. ``NewType`` costs nothing at runtime and
makes mypy refuse the transposition (docs/CODING_STANDARDS.md section 4).
"""

from __future__ import annotations

from typing import NewType
from uuid import UUID

__all__ = ["MasterId", "SalonId", "ServiceId", "UserId"]

SalonId = NewType("SalonId", UUID)
MasterId = NewType("MasterId", UUID)
# A service in the domain sense -- a haircut -- never one of the five services
# of the platform. docs/CODING_STANDARDS.md section 3 keeps the two apart by
# always spelling this one out as ``service_id`` or ``catalog_service``.
ServiceId = NewType("ServiceId", UUID)
# Owned by auth. It appears here as the account behind a master profile and
# never as a foreign key: no constraint of this database may point at another
# service (docs/03-services.md).
UserId = NewType("UserId", UUID)
