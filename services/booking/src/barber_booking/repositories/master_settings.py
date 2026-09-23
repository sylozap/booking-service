"""The settings rows of masters."""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from barber_booking.domain.identifiers import MasterId, SalonId, UserId
from barber_booking.domain.master import MasterSettings
from barber_booking.models.master_settings import MasterSettings as MasterSettingsRow

__all__ = ["MasterSettingsRepository"]


def _to_domain(row: MasterSettingsRow) -> MasterSettings:
    return MasterSettings(
        master_id=MasterId(row.master_id),
        salon_id=SalonId(row.salon_id),
        user_id=UserId(row.user_id),
        timezone=row.timezone,
        buffer_after_min=row.buffer_after_min,
        is_active=row.is_active,
    )


class MasterSettingsRepository:
    """Access to ``master_settings``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, master_id: MasterId) -> MasterSettings | None:
        row = await self._session.get(MasterSettingsRow, master_id)
        return None if row is None else _to_domain(row)

    async def lock(self, master_id: MasterId) -> MasterSettings | None:
        """Read the row and hold it until the transaction ends.

        Every change of one master's schedule takes this lock first, so two
        of them never validate against the same state and both write.
        """
        statement = (
            select(MasterSettingsRow)
            .where(MasterSettingsRow.master_id == master_id)
            .with_for_update()
        )
        row = (await self._session.execute(statement)).scalar_one_or_none()
        return None if row is None else _to_domain(row)

    async def set_buffer(self, master_id: MasterId, buffer_after_min: int) -> MasterSettings:
        statement = (
            update(MasterSettingsRow)
            .where(MasterSettingsRow.master_id == master_id)
            .values(buffer_after_min=buffer_after_min)
            .returning(MasterSettingsRow)
        )
        row = (await self._session.execute(statement)).scalar_one()
        return _to_domain(row)
