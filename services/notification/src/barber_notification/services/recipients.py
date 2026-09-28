"""Keeping the recipients in step with the accounts in auth.

``auth.users.v1`` is keyed by user, so its events arrive in order as long as
the relay publishes them in order -- which two relay replicas do not promise.
Every rule here therefore survives a late, older event:

* a contact snapshot applies only if it was taken after the one applied last;
* a confirmation counts only for the address it confirmed;
* a deactivation is final, since no event brings an account back.

Nothing about a person is logged beyond the user id.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from barber_common.db.session import transaction
from barber_common.events.users import (
    UserContactsUpdated,
    UserDeactivated,
    UserEmailConfirmed,
    UserRegistered,
)
from barber_common.logging import get_logger
from barber_notification.repositories.recipients import RecipientRecord, RecipientRepository

__all__ = ["FollowUser", "apply_confirmation", "apply_contacts"]

_logger = get_logger(__name__)


def apply_contacts(
    recipient: RecipientRecord,
    *,
    email: str,
    phone: str,
    email_confirmed: bool,
    taken_at: datetime,
) -> RecipientRecord:
    """The recipient after a contact snapshot taken at ``taken_at``.

    An older snapshot changes nothing. A confirmation already recorded for the
    same address survives a snapshot that predates it.
    """
    if recipient.contacts_updated_at is not None and recipient.contacts_updated_at >= taken_at:
        return recipient

    still_confirmed = recipient.email == email and recipient.email_confirmed
    return replace(
        recipient,
        email=email,
        phone=phone,
        email_confirmed=email_confirmed or still_confirmed,
        contacts_updated_at=taken_at,
    )


def apply_confirmation(recipient: RecipientRecord, *, email: str) -> RecipientRecord:
    """The recipient after ``email`` was confirmed.

    The address is taken over when none is known yet, so a confirmation that
    overtook the registration is not lost. For an address changed since, it
    confirms nothing.
    """
    if recipient.email is None:
        return replace(recipient, email=email, email_confirmed=True)
    if recipient.email != email:
        return recipient
    return replace(recipient, email_confirmed=True)


class FollowUser:
    """Apply what auth announces about an account to its recipient row."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._recipients = RecipientRepository(session)

    async def registered(self, event: UserRegistered, *, occurred_at: datetime) -> None:
        await self._apply_contacts(
            event.user_id,
            email=event.email,
            phone=event.phone,
            email_confirmed=event.email_confirmed,
            taken_at=occurred_at,
        )

    async def contacts_updated(self, event: UserContactsUpdated, *, occurred_at: datetime) -> None:
        await self._apply_contacts(
            event.user_id,
            email=event.email,
            phone=event.phone,
            email_confirmed=event.email_confirmed,
            taken_at=occurred_at,
        )

    async def email_confirmed(self, event: UserEmailConfirmed) -> None:
        async with transaction(self._session):
            current = await self._recipients.lock_or_create(event.user_id)
            changed = apply_confirmation(current, email=event.email)
            if changed != current:
                await self._recipients.save(changed)
        _logger.info(
            "recipient email confirmation applied",
            user_id=str(event.user_id),
            applied=changed != current,
        )

    async def deactivated(self, event: UserDeactivated) -> None:
        """Nothing is sent to the account from now on."""
        async with transaction(self._session):
            current = await self._recipients.lock_or_create(event.user_id)
            if current.is_active:
                await self._recipients.save(replace(current, is_active=False))
        _logger.info("recipient deactivated", user_id=str(event.user_id))

    async def _apply_contacts(
        self,
        user_id: UUID,
        *,
        email: str,
        phone: str,
        email_confirmed: bool,
        taken_at: datetime,
    ) -> None:
        async with transaction(self._session):
            current = await self._recipients.lock_or_create(user_id)
            changed = apply_contacts(
                current,
                email=email,
                phone=phone,
                email_confirmed=email_confirmed,
                taken_at=taken_at,
            )
            if changed != current:
                await self._recipients.save(changed)
        _logger.info(
            "recipient contacts applied",
            user_id=str(user_id),
            applied=changed != current,
        )
