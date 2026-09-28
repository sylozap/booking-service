"""Keeping the settings of masters in step with catalog.

A master's settings row is created by ``master.created`` and follows
``master.updated`` from then on. A master whose creation booking never heard
of -- created before the consumer ran, or with the event long gone from the
topic -- gets the row the first time their schedule is touched, from the
profile catalog describes.

Every write here tolerates the other path having been first: the event and the
lazy read can race for one master, and both leave one row behind.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from barber_booking.clients.catalog import CatalogClient
from barber_booking.domain.identifiers import MasterId, SalonId, UserId
from barber_booking.domain.master import (
    DEFAULT_BUFFER_AFTER_MIN,
    MasterSettings,
    validate_timezone,
)
from barber_booking.repositories.master_settings import MasterSettingsRepository
from barber_common.db.session import transaction
from barber_common.events.catalog import MasterCreated, MasterUpdated
from barber_common.logging import get_logger

__all__ = ["EnsureMasterSettings", "FollowMaster"]

_logger = get_logger(__name__)


class FollowMaster:
    """Apply what catalog announces about a master to their settings row."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._masters = MasterSettingsRepository(session)

    async def created(self, event: MasterCreated) -> None:
        """Create the row with the default buffer, unless it already exists."""
        async with transaction(self._session):
            created = await self._masters.add_if_absent(
                MasterSettings(
                    master_id=MasterId(event.master_id),
                    salon_id=SalonId(event.salon_id),
                    user_id=UserId(event.user_id),
                    timezone=event.timezone,
                    buffer_after_min=DEFAULT_BUFFER_AFTER_MIN,
                    is_active=event.is_active,
                )
            )
        if created:
            _logger.info("master settings created", master_id=str(event.master_id))

    async def updated(self, event: MasterUpdated) -> None:
        """Take over the zone and the activity from the snapshot.

        A master without a row gets one from the snapshot, which carries
        everything ``master.created`` does. A snapshot from before the zone was
        part of it cannot create one, and the lazy path will.
        """
        master_id = MasterId(event.master_id)
        if event.timezone is not None:
            validate_timezone(event.timezone)

        async with transaction(self._session):
            followed = await self._masters.follow(
                master_id, timezone=event.timezone, is_active=event.is_active
            )
            if followed is not None or event.timezone is None:
                return
            created = await self._masters.add_if_absent(
                MasterSettings(
                    master_id=master_id,
                    salon_id=SalonId(event.salon_id),
                    user_id=UserId(event.user_id),
                    timezone=event.timezone,
                    buffer_after_min=DEFAULT_BUFFER_AFTER_MIN,
                    is_active=event.is_active,
                )
            )
        if created:
            _logger.info("master settings created from a snapshot", master_id=str(master_id))


class EnsureMasterSettings:
    """Make sure a master has a settings row before their schedule is touched.

    Called before the scenario's own transaction opens: the call to catalog is
    a network call, and no connection is held across it.
    """

    def __init__(self, session: AsyncSession, catalog: CatalogClient) -> None:
        self._session = session
        self._masters = MasterSettingsRepository(session)
        self._catalog = catalog

    async def execute(self, master_id: MasterId) -> None:
        """Nothing to do for a known master; a master catalog does not know is 404."""
        async with transaction(self._session):
            if await self._masters.get(master_id) is not None:
                return

        profile = await self._catalog.get_master(master_id=master_id)
        async with transaction(self._session):
            created = await self._masters.add_if_absent(
                MasterSettings(
                    master_id=MasterId(profile.master_id),
                    salon_id=SalonId(profile.salon_id),
                    user_id=UserId(profile.user_id),
                    timezone=profile.timezone,
                    buffer_after_min=DEFAULT_BUFFER_AFTER_MIN,
                    is_active=profile.is_active,
                )
            )
        if created:
            _logger.info("master settings created on first use", master_id=str(master_id))
