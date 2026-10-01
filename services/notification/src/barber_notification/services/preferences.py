"""Reading and changing a user's notification switches.

Their own only: the user is the subject of the token, never a parameter. A user
this service has not heard of yet -- the events of auth may be late -- reads
the defaults, and a change creates the row the events will fill in later.
"""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from barber_common.db.session import transaction
from barber_common.logging import get_logger
from barber_notification.preferences import PreferenceChanges, Preferences
from barber_notification.repositories.recipients import RecipientRepository

__all__ = ["ReadPreferences", "UpdatePreferences"]

_logger = get_logger(__name__)


class ReadPreferences:
    """The switches of a user, every one on if none was ever turned off."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._recipients = RecipientRepository(session)

    async def execute(self, user_id: UUID) -> Preferences:
        async with transaction(self._session):
            recipient = await self._recipients.get(user_id)
        if recipient is None:
            return Preferences()
        return Preferences.from_stored(recipient.preferences)


class UpdatePreferences:
    """Flip the switches named, and only those. Returns the whole matrix after."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._recipients = RecipientRepository(session)

    async def execute(self, user_id: UUID, changes: PreferenceChanges) -> Preferences:
        async with transaction(self._session):
            # Locked: two requests changing different switches must both land.
            current = await self._recipients.lock_or_create(user_id)
            updated = Preferences.from_stored(current.preferences).with_changes(changes)
            await self._recipients.save(replace(current, preferences=updated.to_stored()))

        _logger.info("notification preferences updated", user_id=str(user_id))
        return updated
