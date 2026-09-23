"""Identifiers of the service, distinct at the type level.

A booking names a master, a salon, a client and a service, all UUIDs.
``NewType`` makes mypy refuse passing one where another is expected.
"""

from __future__ import annotations

from typing import NewType
from uuid import UUID

__all__ = [
    "BookingId",
    "ExceptionId",
    "MasterId",
    "SalonId",
    "ServiceId",
    "UserId",
]

BookingId = NewType("BookingId", UUID)
MasterId = NewType("MasterId", UUID)
SalonId = NewType("SalonId", UUID)
# A service in the domain sense -- a haircut -- never one of the platform's
# microservices.
ServiceId = NewType("ServiceId", UUID)
UserId = NewType("UserId", UUID)
ExceptionId = NewType("ExceptionId", UUID)
