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

from barber_catalog.services.catalog_services import (
    ArchiveService,
    CreateService,
    ListSalonServices,
    ReadService,
    UpdateService,
)
from barber_catalog.services.master_services import (
    LinkMasterService,
    UnlinkMasterService,
)
from barber_catalog.services.masters import (
    CreateMaster,
    ListSalonMasters,
    ReadMasterCard,
    UpdateMaster,
)
from barber_catalog.services.salons import CreateSalon, ListSalons, ReadSalon, UpdateSalon
from barber_common.db.session import get_session

__all__ = [
    "ArchiveServiceScenario",
    "CreateMasterScenario",
    "CreateSalonScenario",
    "CreateServiceScenario",
    "LinkMasterServiceScenario",
    "ListSalonMastersScenario",
    "ListSalonServicesScenario",
    "ListSalonsScenario",
    "ReadMasterCardScenario",
    "ReadSalonScenario",
    "ReadServiceScenario",
    "SessionDependency",
    "UnlinkMasterServiceScenario",
    "UpdateMasterScenario",
    "UpdateSalonScenario",
    "UpdateServiceScenario",
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


def build_create_service(session: SessionDependency) -> CreateService:
    """The service creation scenario for this request."""
    return CreateService(session)


def build_update_service(session: SessionDependency) -> UpdateService:
    """The service update scenario for this request."""
    return UpdateService(session)


def build_archive_service(session: SessionDependency) -> ArchiveService:
    """The service archiving scenario for this request."""
    return ArchiveService(session)


def build_read_service(session: SessionDependency) -> ReadService:
    """The single service read scenario for this request."""
    return ReadService(session)


def build_list_salon_services(session: SessionDependency) -> ListSalonServices:
    """The price list scenario for this request."""
    return ListSalonServices(session)


CreateServiceScenario = Annotated[CreateService, Depends(build_create_service)]
UpdateServiceScenario = Annotated[UpdateService, Depends(build_update_service)]
ArchiveServiceScenario = Annotated[ArchiveService, Depends(build_archive_service)]
ReadServiceScenario = Annotated[ReadService, Depends(build_read_service)]
ListSalonServicesScenario = Annotated[ListSalonServices, Depends(build_list_salon_services)]


def build_link_master_service(session: SessionDependency) -> LinkMasterService:
    """The offering scenario for this request."""
    return LinkMasterService(session)


def build_unlink_master_service(session: SessionDependency) -> UnlinkMasterService:
    """The withdrawal scenario for this request."""
    return UnlinkMasterService(session)


LinkMasterServiceScenario = Annotated[LinkMasterService, Depends(build_link_master_service)]
UnlinkMasterServiceScenario = Annotated[UnlinkMasterService, Depends(build_unlink_master_service)]
