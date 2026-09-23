"""Assembling the scenarios a request needs.

Builds each scenario from the session and the cache of the running service.
Writers take the cache too, because they invalidate it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from barber_catalog.services.cache import CatalogCache
from barber_catalog.services.catalog_services import (
    ArchiveService,
    CreateService,
    ListSalonServices,
    ReadService,
    UpdateService,
)
from barber_catalog.services.master_profile import ReadMasterProfile
from barber_catalog.services.master_service_details import ReadMasterServiceDetails
from barber_catalog.services.master_services import (
    LinkMasterService,
    UnlinkMasterService,
)
from barber_catalog.services.masters import (
    ActivateMaster,
    CreateMaster,
    DeactivateMaster,
    ListSalonMasters,
    ReadMasterCard,
    UpdateMaster,
)
from barber_catalog.services.salons import CreateSalon, ListSalons, ReadSalon, UpdateSalon
from barber_common.cache import Cache
from barber_common.db.session import get_session

__all__ = [
    "ActivateMasterScenario",
    "ArchiveServiceScenario",
    "CacheDependency",
    "CreateMasterScenario",
    "CreateSalonScenario",
    "CreateServiceScenario",
    "DeactivateMasterScenario",
    "LinkMasterServiceScenario",
    "ListSalonMastersScenario",
    "ListSalonServicesScenario",
    "ListSalonsScenario",
    "ReadMasterCardScenario",
    "ReadMasterProfileScenario",
    "ReadMasterServiceDetailsScenario",
    "ReadSalonScenario",
    "ReadServiceScenario",
    "SessionDependency",
    "UnlinkMasterServiceScenario",
    "UpdateMasterScenario",
    "UpdateSalonScenario",
    "UpdateServiceScenario",
]

SessionDependency = Annotated[AsyncSession, Depends(get_session)]


def get_catalog_cache(request: Request) -> CatalogCache:
    """The cache of the running service, wrapped in the catalog key scheme.

    Falls back to a disabled cache when the lifespan has not run, since the
    service works without one.
    """
    cache = getattr(request.app.state, "cache", None)
    if not isinstance(cache, Cache):
        return CatalogCache(Cache.disabled())
    return CatalogCache(cache)


CacheDependency = Annotated[CatalogCache, Depends(get_catalog_cache)]


def build_create_salon(session: SessionDependency, cache: CacheDependency) -> CreateSalon:
    """The salon creation scenario for this request."""
    return CreateSalon(session, cache)


def build_update_salon(session: SessionDependency, cache: CacheDependency) -> UpdateSalon:
    """The salon update scenario for this request."""
    return UpdateSalon(session, cache)


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


def build_create_master(session: SessionDependency, cache: CacheDependency) -> CreateMaster:
    """The master creation scenario for this request."""
    return CreateMaster(session, cache)


def build_update_master(session: SessionDependency, cache: CacheDependency) -> UpdateMaster:
    """The master update scenario for this request."""
    return UpdateMaster(session, cache)


def build_read_master_card(session: SessionDependency, cache: CacheDependency) -> ReadMasterCard:
    """The master card scenario for this request."""
    return ReadMasterCard(session, cache)


def build_list_salon_masters(session: SessionDependency) -> ListSalonMasters:
    """The salon staff listing scenario for this request."""
    return ListSalonMasters(session)


def build_deactivate_master(session: SessionDependency, cache: CacheDependency) -> DeactivateMaster:
    """The deactivation scenario for this request."""
    return DeactivateMaster(session, cache)


def build_activate_master(session: SessionDependency, cache: CacheDependency) -> ActivateMaster:
    """The reactivation scenario for this request."""
    return ActivateMaster(session, cache)


CreateMasterScenario = Annotated[CreateMaster, Depends(build_create_master)]
DeactivateMasterScenario = Annotated[DeactivateMaster, Depends(build_deactivate_master)]
ActivateMasterScenario = Annotated[ActivateMaster, Depends(build_activate_master)]
UpdateMasterScenario = Annotated[UpdateMaster, Depends(build_update_master)]
ReadMasterCardScenario = Annotated[ReadMasterCard, Depends(build_read_master_card)]
ListSalonMastersScenario = Annotated[ListSalonMasters, Depends(build_list_salon_masters)]


def build_create_service(session: SessionDependency, cache: CacheDependency) -> CreateService:
    """The service creation scenario for this request."""
    return CreateService(session, cache)


def build_update_service(session: SessionDependency, cache: CacheDependency) -> UpdateService:
    """The service update scenario for this request."""
    return UpdateService(session, cache)


def build_archive_service(session: SessionDependency, cache: CacheDependency) -> ArchiveService:
    """The service archiving scenario for this request."""
    return ArchiveService(session, cache)


def build_read_service(session: SessionDependency, cache: CacheDependency) -> ReadService:
    """The single service read scenario for this request."""
    return ReadService(session, cache)


def build_list_salon_services(session: SessionDependency) -> ListSalonServices:
    """The price list scenario for this request."""
    return ListSalonServices(session)


CreateServiceScenario = Annotated[CreateService, Depends(build_create_service)]
UpdateServiceScenario = Annotated[UpdateService, Depends(build_update_service)]
ArchiveServiceScenario = Annotated[ArchiveService, Depends(build_archive_service)]
ReadServiceScenario = Annotated[ReadService, Depends(build_read_service)]
ListSalonServicesScenario = Annotated[ListSalonServices, Depends(build_list_salon_services)]


def build_link_master_service(
    session: SessionDependency, cache: CacheDependency
) -> LinkMasterService:
    """The offering scenario for this request."""
    return LinkMasterService(session, cache)


def build_unlink_master_service(
    session: SessionDependency, cache: CacheDependency
) -> UnlinkMasterService:
    """The withdrawal scenario for this request."""
    return UnlinkMasterService(session, cache)


LinkMasterServiceScenario = Annotated[LinkMasterService, Depends(build_link_master_service)]
UnlinkMasterServiceScenario = Annotated[UnlinkMasterService, Depends(build_unlink_master_service)]


def build_read_master_service_details(
    session: SessionDependency, cache: CacheDependency
) -> ReadMasterServiceDetails:
    """The internal catalog read scenario for this request."""
    return ReadMasterServiceDetails(session, cache)


ReadMasterServiceDetailsScenario = Annotated[
    ReadMasterServiceDetails, Depends(build_read_master_service_details)
]


def build_read_master_profile(session: SessionDependency) -> ReadMasterProfile:
    return ReadMasterProfile(session)


ReadMasterProfileScenario = Annotated[ReadMasterProfile, Depends(build_read_master_profile)]
