"""Creating, changing and reading master profiles.

**Creating a master publishes an event, and it does so through the outbox.**
``booking`` needs a ``master_settings`` row before a schedule can be written
against the profile (docs/03-services.md), and that row is created by
``master.created``. The event is written in the same transaction as the profile
itself: publishing to the broker after the commit would lose the event whenever
the broker blinks, and publishing before it would announce a master that the
rollback then took away (ADR-0004).

**The account behind a profile is not checked.** ``user_id`` names a row in
``auth`` and no call is made to find out whether it is there. A synchronous
cross-service validation on a write path buys very little -- the account can be
deleted a second later anyway -- and costs the availability of ``catalog``
whenever ``auth`` is slow. A profile naming an account that does not exist is a
data error, not a broken system (T2.3).
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
)
from barber_common.logging import get_logger
from barber_common.outbox import OutboxRepository
from barber_common.pagination import Page, PageRequest

__all__ = [
    "ActivateMaster",
    "CreateMaster",
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


def offered_service(link: MasterService) -> OfferedServiceResponse:
    """Map one link to the card entry, resolving the final figures.

    The resolution goes through :class:`Offering` and never through a
    ``coalesce`` written here: the internal endpoint of T2.6 answers the same
    question for ``booking``, and two copies of the rule are two answers
    waiting to differ.

    Requires ``link.service`` to be loaded -- the relationship raises rather
    than lazily loading, and the repository that hands these over says so.
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

        The salon is loaded first because two things need it: the scope check,
        which is a question about this salon, and the time zone, which travels
        in the event so that ``booking`` can build ``master_settings`` without
        calling back.
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
                # The constraint is named, not merely the SQLSTATE: this table
                # could grow a second unique index tomorrow, and reporting
                # every 23505 as "this master already exists" would then be
                # wrong in a way nobody notices
                # (docs/CODING_STANDARDS.md section 8).
                if not is_unique_violation(error, constraint="uq_masters_user_id_salon_id"):
                    raise
                raise MasterProfileExists(
                    "This account already has a profile in this salon"
                ) from error

            # Same transaction as the row above. The partitioning key is the
            # master, so everything that ever happens to this profile stays in
            # order on the topic (docs/07-events-and-kafka.md).
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
        self._cache = cache

    async def execute(
        self,
        *,
        caller: Principal,
        master_id: MasterId,
        body: MasterUpdateRequest,
    ) -> MasterResponse:
        """Apply the fields the caller actually sent.

        No event here. ``master.updated`` belongs to T2.9, and publishing one
        now would announce a schema no consumer has agreed to yet.
        """
        async with transaction(self._session):
            master = await self._masters.get(master_id)
            if master is None:
                raise MasterNotFound("No such master")

            require_salon_scope(caller, master.salon_id)

            for field, value in body.model_dump(exclude_unset=True).items():
                setattr(master, field, value)
            await self._session.flush()
            response = master_response(master)

        await self._cache.invalidate()

        _logger.info("master updated", master_id=str(master.id), updated_by=caller.subject)
        return response


class ReadMasterCard:
    """One master, with everything they offer, for anybody who asks."""

    def __init__(self, session: AsyncSession, cache: CatalogCache) -> None:
        self._masters = MasterRepository(session)
        self._cache = cache

    async def execute(self, *, master_id: MasterId) -> MasterCardResponse:
        """The profile and its services, each at the price this master charges.

        Read through the cache: this is what a visitor opens when they pick a
        master, and building it costs three statements with their joins. A miss
        -- or a cache that is unreachable -- falls through to the database and
        answers identically.

        Services are ordered by name so the card is stable between requests: an
        unordered listing reshuffles itself whenever PostgreSQL feels like it,
        and a visitor comparing two masters sees the difference as noise.
        """
        key = await self._cache.master_card_key(master_id)
        cached = await self._cache.read(key, MasterCardResponse)
        if cached is not None:
            return cached

        master = await self._masters.get_with_offerings(master_id)
        if master is None:
            # Deliberately not cached. A card that does not exist yet is asked
            # for by a client that is about to be told to create it, and
            # storing the absence would make the creation invisible for the
            # rest of the window.
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

        **The consequences are not local.** ``booking`` consumes this event and
        cancels every future booking of this master (T4.6), which is why the
        endpoint answers ``202`` rather than ``200``: the row is written, the
        cancellations are not, and telling the caller otherwise would be a lie
        it can observe by immediately reading the bookings.

        Repeating the call writes nothing and queues nothing. Idempotence here
        is not a nicety: a second event would make ``booking`` cancel a second
        time, and a client whose booking was reinstated in between would lose
        it again without anyone having asked.
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
        self._cache = cache

    async def execute(self, *, caller: Principal, master_id: MasterId) -> MasterResponse:
        """Mark the master active again.

        **Nothing is restored.** The bookings cancelled when they were
        deactivated stay cancelled: the clients were told, and the slots have
        been open to everyone else since. Reinstating them would double-book
        the ones that were taken in the meantime -- which is the invariant the
        whole platform is built around -- and silently reinstating the rest
        would give clients an appointment they were told they no longer had.
        Rebooking is a client's decision, not a side effect of an
        administrator's.

        Repeating the call changes nothing and still succeeds: the requested
        state is the state that holds.

        No event yet. ``master.updated`` arrives with T2.9, and publishing an
        event no consumer has agreed to a schema for buys nothing.
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
            response = master_response(master)

        await self._cache.invalidate()

        _logger.info("master activated", master_id=str(master.id), activated_by=caller.subject)
        return response
