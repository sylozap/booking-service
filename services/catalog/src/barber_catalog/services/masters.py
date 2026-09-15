"""Creating, changing and reading master profiles.

Creating a master writes ``master.created`` to the outbox in the same
transaction. ``user_id`` is not checked against ``auth``.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from barber_catalog.domain.errors import MasterNotFound, MasterProfileExists, SalonNotFound
from barber_catalog.domain.identifiers import MasterId, SalonId
from barber_catalog.domain.pricing import Offering
from barber_catalog.models.master import Master
from barber_catalog.models.master_service import MasterService
from barber_catalog.repositories.masters import (
    MASTER_CURSOR_ARITY,
    MasterRepository,
    master_cursor,
)
from barber_catalog.repositories.salons import SalonRepository
from barber_catalog.schemas.masters import (
    MasterCardResponse,
    MasterCreateRequest,
    MasterResponse,
    MasterUpdateRequest,
    OfferedServiceResponse,
)
from barber_catalog.services.authorization import require_salon_scope
from barber_catalog.services.cache import CatalogCache
from barber_common.auth import Principal
from barber_common.db.errors import is_unique_violation
from barber_common.db.session import transaction
from barber_common.events.catalog import (
    CATALOG_MASTERS_TOPIC,
    MASTER_AGGREGATE_TYPE,
    MasterCreated,
    MasterDeactivated,
    MasterEventType,
    MasterUpdated,
)
from barber_common.logging import get_logger
from barber_common.outbox import OutboxRepository
from barber_common.pagination import Page, PageRequest

__all__ = [
    "ActivateMaster",
    "CreateMaster",
    "master_snapshot",
    "DeactivateMaster",
    "ListSalonMasters",
    "ReadMasterCard",
    "UpdateMaster",
    "master_response",
    "offered_service",
]

_logger = get_logger(__name__)


def master_response(master: Master) -> MasterResponse:
    """Map a profile row to the response body."""
    return MasterResponse(
        id=master.id,
        salon_id=master.salon_id,
        user_id=master.user_id,
        display_name=master.display_name,
        bio=master.bio,
        photo_url=master.photo_url,
        specialization=master.specialization,
        is_active=master.is_active,
        created_at=master.created_at,
        updated_at=master.updated_at,
    )


def master_snapshot(master: Master) -> MasterUpdated:
    """The whole of a master, as ``master.updated`` reports it."""
    return MasterUpdated(
        master_id=master.id,
        salon_id=master.salon_id,
        user_id=master.user_id,
        display_name=master.display_name,
        specialization=master.specialization,
        is_active=master.is_active,
    )


def offered_service(link: MasterService) -> OfferedServiceResponse:
    """Map one link to the card entry, resolving the final figures.

    Resolved through :class:`Offering`. Requires ``link.service`` to be loaded.
    """
    offering = Offering(
        base_price=link.service.base_price,
        base_duration_min=link.service.base_duration_min,
        price_override=link.price_override,
        duration_override=link.duration_override,
    )
    return OfferedServiceResponse(
        service_id=link.service_id,
        name=link.service.name,
        description=link.service.description,
        price=offering.price,
        currency=link.service.currency,
        duration_min=offering.duration_min,
        base_price=link.service.base_price,
        base_duration_min=link.service.base_duration_min,
    )


class CreateMaster:
    """Add a master to a salon and announce it."""

    def __init__(self, session: AsyncSession, cache: CatalogCache) -> None:
        self._session = session
        self._masters = MasterRepository(session)
        self._salons = SalonRepository(session)
        self._outbox = OutboxRepository(session)
        self._cache = cache

    async def execute(self, *, caller: Principal, body: MasterCreateRequest) -> MasterResponse:
        """Create the profile and queue ``master.created`` beside it.

        The salon is loaded for the scope check and for the time zone the event
        carries.
        """
        async with transaction(self._session):
            salon = await self._salons.get(SalonId(body.salon_id))
            if salon is None:
                raise SalonNotFound("No such salon")

            require_salon_scope(caller, salon.id)

            try:
                master = await self._masters.add(
                    Master(
                        salon_id=salon.id,
                        user_id=body.user_id,
                        display_name=body.display_name,
                        bio=body.bio,
                        photo_url=body.photo_url,
                        specialization=body.specialization,
                    )
                )
            except IntegrityError as error:
                # Matched by constraint name, not only by SQLSTATE, so another
                # unique index is not misreported.
                if not is_unique_violation(error, constraint="uq_masters_user_id_salon_id"):
                    raise
                raise MasterProfileExists(
                    "This account already has a profile in this salon"
                ) from error

            # Same transaction as the row above, keyed by the master.
            await self._outbox.add(
                topic=CATALOG_MASTERS_TOPIC,
                aggregate_type=MASTER_AGGREGATE_TYPE,
                aggregate_id=master.id,
                event_type=MasterEventType.CREATED.value,
                payload=MasterCreated(
                    master_id=master.id,
                    salon_id=salon.id,
                    user_id=master.user_id,
                    display_name=master.display_name,
                    timezone=salon.timezone,
                    is_active=master.is_active,
                ),
            )

            response = master_response(master)

        await self._cache.invalidate()

        _logger.info("master created", master_id=str(master.id), created_by=caller.subject)
        return response


class UpdateMaster:
    """Change a profile."""

    def __init__(self, session: AsyncSession, cache: CatalogCache) -> None:
        self._session = session
        self._masters = MasterRepository(session)
        self._outbox = OutboxRepository(session)
        self._cache = cache

    async def execute(
        self,
        *,
        caller: Principal,
        master_id: MasterId,
        body: MasterUpdateRequest,
    ) -> MasterResponse:
        """Apply the fields the caller actually sent, and announce the change.

        A request that changes nothing publishes nothing.
        """
        async with transaction(self._session):
            master = await self._masters.get(master_id)
            if master is None:
                raise MasterNotFound("No such master")

            require_salon_scope(caller, master.salon_id)

            changes = {
                field: value
                for field, value in body.model_dump(exclude_unset=True).items()
                if getattr(master, field) != value
            }
            if not changes:
                return master_response(master)

            for field, value in changes.items():
                setattr(master, field, value)
            await self._session.flush()

            await self._outbox.add(
                topic=CATALOG_MASTERS_TOPIC,
                aggregate_type=MASTER_AGGREGATE_TYPE,
                aggregate_id=master.id,
                event_type=MasterEventType.UPDATED.value,
                payload=master_snapshot(master),
            )
            response = master_response(master)

        await self._cache.invalidate()

        _logger.info(
            "master updated",
            master_id=str(master.id),
            updated_by=caller.subject,
            changed_fields=sorted(changes),
        )
        return response


class ReadMasterCard:
    """One master, with everything they offer, for anybody who asks."""

    def __init__(self, session: AsyncSession, cache: CatalogCache) -> None:
        self._masters = MasterRepository(session)
        self._cache = cache

    async def execute(self, *, master_id: MasterId) -> MasterCardResponse:
        """The profile and its services, each at the price this master charges.

        Read through the cache. Services are ordered by name.
        """
        key = await self._cache.master_card_key(master_id)
        cached = await self._cache.read(key, MasterCardResponse)
        if cached is not None:
            return cached

        master = await self._masters.get_with_offerings(master_id)
        if master is None:
            # A missing card is not cached, so a profile created right after is
            # visible immediately.
            raise MasterNotFound("No such master")

        services = sorted(
            (offered_service(link) for link in master.offerings),
            key=lambda offered: (offered.name, str(offered.service_id)),
        )
        card = MasterCardResponse(
            **master_response(master).model_dump(),
            services=services,
        )

        await self._cache.write(key, card)
        return card


class ListSalonMasters:
    """The masters of one salon, one page at a time."""

    def __init__(self, session: AsyncSession) -> None:
        self._masters = MasterRepository(session)
        self._salons = SalonRepository(session)

    async def execute(
        self,
        *,
        salon_id: SalonId,
        request: PageRequest,
        is_active: bool | None = None,
    ) -> Page[MasterResponse]:
        """One page of the staff of a salon, ordered by name.

        The salon is checked first so that an unknown identifier answers
        ``404`` rather than an empty page: the two mean different things, and a
        client cannot tell a salon with no masters from a typo otherwise.
        """
        if await self._salons.get(salon_id) is None:
            raise SalonNotFound("No such salon")

        request.key(arity=MASTER_CURSOR_ARITY)
        rows: Sequence[Master] = await self._masters.page(
            salon_id=salon_id, request=request, is_active=is_active
        )

        page = Page.of(rows, request=request, cursor_of=master_cursor)
        return Page[MasterResponse](
            items=[master_response(master) for master in page.items],
            next_cursor=page.next_cursor,
        )


class DeactivateMaster:
    """Stop a master working, and let ``booking`` cancel what they had booked."""

    def __init__(self, session: AsyncSession, cache: CatalogCache) -> None:
        self._session = session
        self._masters = MasterRepository(session)
        self._outbox = OutboxRepository(session)
        self._cache = cache

    async def execute(self, *, caller: Principal, master_id: MasterId) -> MasterResponse:
        """Mark the master inactive and queue ``master.deactivated`` beside it.

        ``booking`` cancels the master's future bookings asynchronously, hence
        ``202``. Repeating the call writes and queues nothing.
        """
        async with transaction(self._session):
            master = await self._masters.get(master_id)
            if master is None:
                raise MasterNotFound("No such master")

            require_salon_scope(caller, master.salon_id)

            if not master.is_active:
                return master_response(master)

            master.is_active = False
            await self._outbox.add(
                topic=CATALOG_MASTERS_TOPIC,
                aggregate_type=MASTER_AGGREGATE_TYPE,
                aggregate_id=master.id,
                event_type=MasterEventType.DEACTIVATED.value,
                payload=MasterDeactivated(master_id=master.id, salon_id=master.salon_id),
            )
            await self._session.flush()
            response = master_response(master)

        await self._cache.invalidate()

        _logger.info(
            "master deactivated",
            master_id=str(master.id),
            deactivated_by=caller.subject,
        )
        return response


class ActivateMaster:
    """Put a master back to work."""

    def __init__(self, session: AsyncSession, cache: CatalogCache) -> None:
        self._session = session
        self._masters = MasterRepository(session)
        self._outbox = OutboxRepository(session)
        self._cache = cache

    async def execute(self, *, caller: Principal, master_id: MasterId) -> MasterResponse:
        """Mark the master active again and publish ``master.updated``.

        Bookings cancelled on deactivation are not restored. Repeating the call
        changes and publishes nothing.
        """
        async with transaction(self._session):
            master = await self._masters.get(master_id)
            if master is None:
                raise MasterNotFound("No such master")

            require_salon_scope(caller, master.salon_id)

            if master.is_active:
                return master_response(master)

            master.is_active = True
            await self._session.flush()

            await self._outbox.add(
                topic=CATALOG_MASTERS_TOPIC,
                aggregate_type=MASTER_AGGREGATE_TYPE,
                aggregate_id=master.id,
                event_type=MasterEventType.UPDATED.value,
                payload=master_snapshot(master),
            )
            response = master_response(master)

        await self._cache.invalidate()

        _logger.info("master activated", master_id=str(master.id), activated_by=caller.subject)
        return response
