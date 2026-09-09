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

from barber_catalog.services.masters import (
    CreateMaster,
    ListSalonMasters,
    ReadMasterCard,
    UpdateMaster,
)
from barber_catalog.services.salons import CreateSalon, ListSalons, ReadSalon, UpdateSalon
from barber_common.db.session import get_session

__all__ = [
    "CreateMasterScenario",
    "CreateSalonScenario",
    "ListSalonMastersScenario",
    "ListSalonsScenario",
    "ReadMasterCardScenario",
    "ReadSalonScenario",
    "SessionDependency",
    "UpdateMasterScenario",
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


def build_create_master(session: SessionDependency) -> CreateMaster:
    """The master creation scenario for this request."""
    return CreateMaster(session)


def build_update_master(session: SessionDependency) -> UpdateMaster:
    """The master update scenario for this request."""
    return UpdateMaster(session)


def build_read_master_card(session: SessionDependency) -> ReadMasterCard:
    """The master card scenario for this request."""
    return ReadMasterCard(session)


def build_list_salon_masters(session: SessionDependency) -> ListSalonMasters:
    """The salon staff listing scenario for this request."""
    return ListSalonMasters(session)


CreateMasterScenario = Annotated[CreateMaster, Depends(build_create_master)]
UpdateMasterScenario = Annotated[UpdateMaster, Depends(build_update_master)]
ReadMasterCardScenario = Annotated[ReadMasterCard, Depends(build_read_master_card)]
ListSalonMastersScenario = Annotated[ListSalonMasters, Depends(build_list_salon_masters)]
