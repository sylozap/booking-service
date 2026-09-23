"""Assembling the scenarios a request needs.

A scenario takes its dependencies through the constructor, so it can be built
in a test without an application behind it. Here they are put together from
the request's session and the running service's cache.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from barber_booking.clients.catalog import CatalogClient
from barber_booking.services.availability import ReadAvailability
from barber_booking.services.cache import BookingCache
from barber_booking.services.cancel_booking import CancelBooking
from barber_booking.services.create_booking import CreateBooking
from barber_booking.services.master_settings import UpdateMasterSettings
from barber_booking.services.schedule import (
    AddScheduleException,
    ListScheduleExceptions,
    ReadWeeklySchedule,
    RemoveScheduleException,
    ReplaceWeeklySchedule,
)
from barber_common.cache import Cache
from barber_common.db.session import get_session

__all__ = [
    "AddScheduleExceptionScenario",
    "CacheDependency",
    "CancelBookingScenario",
    "CatalogDependency",
    "CreateBookingScenario",
    "ListScheduleExceptionsScenario",
    "ReadAvailabilityScenario",
    "ReadWeeklyScheduleScenario",
    "RemoveScheduleExceptionScenario",
    "ReplaceWeeklyScheduleScenario",
    "SessionDependency",
    "UpdateMasterSettingsScenario",
]

SessionDependency = Annotated[AsyncSession, Depends(get_session)]


def get_booking_cache(request: Request) -> BookingCache:
    """The cache of the running service, or a disabled one.

    A missing cache is a state the service works in, so its absence is not a
    reason to refuse a request.
    """
    cache = getattr(request.app.state, "cache", None)
    if not isinstance(cache, Cache):
        return BookingCache(Cache.disabled())
    return BookingCache(cache)


CacheDependency = Annotated[BookingCache, Depends(get_booking_cache)]


def get_catalog_client(request: Request) -> CatalogClient:
    """The client of ``catalog`` the service built at startup.

    Unlike the cache, there is no working without it: availability and a new
    booking both need the duration and the policies it carries.
    """
    catalog = getattr(request.app.state, "catalog", None)
    if not isinstance(catalog, CatalogClient):
        raise RuntimeError(
            "application state has no catalog client: "
            "build one in the lifespan before serving requests"
        )
    return catalog


CatalogDependency = Annotated[CatalogClient, Depends(get_catalog_client)]


def build_read_availability(
    request: Request,
    session: SessionDependency,
    cache: CacheDependency,
    catalog: CatalogDependency,
) -> ReadAvailability:
    settings = request.app.state.settings
    return ReadAvailability(
        session,
        cache,
        catalog,
        cache_ttl_seconds=settings.availability_cache_ttl_seconds,
    )


ReadAvailabilityScenario = Annotated[ReadAvailability, Depends(build_read_availability)]


def build_create_booking(
    request: Request,
    session: SessionDependency,
    cache: CacheDependency,
    catalog: CatalogDependency,
) -> CreateBooking:
    settings = request.app.state.settings
    return CreateBooking(
        session,
        cache,
        catalog,
        idempotency_ttl=timedelta(hours=settings.idempotency_ttl_hours),
        reminder_lead=timedelta(hours=settings.reminder_lead_hours),
    )


CreateBookingScenario = Annotated[CreateBooking, Depends(build_create_booking)]


def build_cancel_booking(session: SessionDependency, cache: CacheDependency) -> CancelBooking:
    return CancelBooking(session, cache)


CancelBookingScenario = Annotated[CancelBooking, Depends(build_cancel_booking)]


def build_replace_weekly_schedule(
    session: SessionDependency, cache: CacheDependency
) -> ReplaceWeeklySchedule:
    return ReplaceWeeklySchedule(session, cache)


def build_read_weekly_schedule(
    session: SessionDependency, cache: CacheDependency
) -> ReadWeeklySchedule:
    return ReadWeeklySchedule(session, cache)


def build_add_schedule_exception(
    session: SessionDependency, cache: CacheDependency
) -> AddScheduleException:
    return AddScheduleException(session, cache)


def build_list_schedule_exceptions(
    session: SessionDependency, cache: CacheDependency
) -> ListScheduleExceptions:
    return ListScheduleExceptions(session, cache)


def build_remove_schedule_exception(
    session: SessionDependency, cache: CacheDependency
) -> RemoveScheduleException:
    return RemoveScheduleException(session, cache)


def build_update_master_settings(
    session: SessionDependency, cache: CacheDependency
) -> UpdateMasterSettings:
    return UpdateMasterSettings(session, cache)


ReplaceWeeklyScheduleScenario = Annotated[
    ReplaceWeeklySchedule, Depends(build_replace_weekly_schedule)
]
ReadWeeklyScheduleScenario = Annotated[ReadWeeklySchedule, Depends(build_read_weekly_schedule)]
AddScheduleExceptionScenario = Annotated[
    AddScheduleException, Depends(build_add_schedule_exception)
]
ListScheduleExceptionsScenario = Annotated[
    ListScheduleExceptions, Depends(build_list_schedule_exceptions)
]
RemoveScheduleExceptionScenario = Annotated[
    RemoveScheduleException, Depends(build_remove_schedule_exception)
]
UpdateMasterSettingsScenario = Annotated[
    UpdateMasterSettings, Depends(build_update_master_settings)
]
