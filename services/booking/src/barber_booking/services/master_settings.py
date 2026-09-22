"""Changing what booking keeps about a master: for now, the buffer."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from barber_booking.domain.errors import MasterNotFound
from barber_booking.domain.identifiers import MasterId
from barber_booking.domain.master import MasterSettings
from barber_booking.repositories.master_settings import MasterSettingsRepository
from barber_booking.schemas.schedule import MasterSettingsResponse, MasterSettingsUpdateRequest
from barber_booking.services.authorization import require_schedule_access
from barber_booking.services.cache import BookingCache
from barber_common.auth import Principal
from barber_common.db.session import transaction

__all__ = ["UpdateMasterSettings", "settings_response"]


def settings_response(master: MasterSettings) -> MasterSettingsResponse:
    return MasterSettingsResponse(
        master_id=master.master_id,
        salon_id=master.salon_id,
        timezone=master.timezone,
        buffer_after_min=master.buffer_after_min,
        is_active=master.is_active,
    )


class UpdateMasterSettings:
    """Set the buffer a master keeps after each booking.

    Applies to bookings made from now on. A booking already made keeps the
    buffer it was made with: it is part of its snapshot.
    """

    def __init__(self, session: AsyncSession, cache: BookingCache) -> None:
        self._session = session
        self._masters = MasterSettingsRepository(session)
        self._cache = cache

    async def execute(
        self, *, caller: Principal, master_id: MasterId, body: MasterSettingsUpdateRequest
    ) -> MasterSettingsResponse:
        async with transaction(self._session):
            master = await self._masters.lock(master_id)
            if master is None:
                raise MasterNotFound("No such master")
            require_schedule_access(caller, master)

            updated = await self._masters.set_buffer(master_id, body.buffer_after_min)

        await self._cache.invalidate_master(master_id)
        return settings_response(updated)
