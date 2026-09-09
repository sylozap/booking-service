"""Assembling the scenarios a request needs.

A scenario takes its dependencies through the constructor, so it can be built
in a test without an application behind it. This module is where the ones a
request has -- its session, and later its cache -- are put together
(docs/CODING_STANDARDS.md section 7).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from barber_catalog.services.salons import CreateSalon, ListSalons, ReadSalon, UpdateSalon
from barber_common.db.session import get_session

__all__ = [
    "CreateSalonScenario",
    "ListSalonsScenario",
    "ReadSalonScenario",
    "SessionDependency",
    "UpdateSalonScenario",
]

SessionDependency = Annotated[AsyncSession, Depends(get_session)]


def build_create_salon(session: SessionDependency) -> CreateSalon:
    """The salon creation scenario for this request."""
    return CreateSalon(session)


def build_update_salon(session: SessionDependency) -> UpdateSalon:
    """The salon update scenario for this request."""
    return UpdateSalon(session)


def build_read_salon(session: SessionDependency) -> ReadSalon:
    """The single salon read scenario for this request."""
    return ReadSalon(session)


def build_list_salons(session: SessionDependency) -> ListSalons:
    """The salon listing scenario for this request."""
    return ListSalons(session)


CreateSalonScenario = Annotated[CreateSalon, Depends(build_create_salon)]
UpdateSalonScenario = Annotated[UpdateSalon, Depends(build_update_salon)]
ReadSalonScenario = Annotated[ReadSalon, Depends(build_read_salon)]
ListSalonsScenario = Annotated[ListSalons, Depends(build_list_salons)]
