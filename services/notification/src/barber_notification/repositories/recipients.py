"""The recipients this service can reach."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from barber_notification.models.recipient import Recipient as RecipientRow

__all__ = ["RecipientRecord", "RecipientRepository"]


@dataclass(frozen=True, slots=True)
class RecipientRecord:
    """One recipient as the scenarios see it: contacts, channels, state."""

    user_id: UUID
    email: str | None
    phone: str | None
    email_confirmed: bool
    telegram_chat_id: int | None
    is_active: bool
    preferences: dict[str, object]
    contacts_updated_at: datetime | None


def _to_record(row: RecipientRow) -> RecipientRecord:
    return RecipientRecord(
        user_id=row.user_id,
        email=row.email,
        phone=row.phone,
        email_confirmed=row.email_confirmed,
        telegram_chat_id=row.telegram_chat_id,
        is_active=row.is_active,
        preferences=dict(row.preferences),
        contacts_updated_at=row.contacts_updated_at,
    )


class RecipientRepository:
    """Access to ``recipients``. Never commits."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: UUID) -> RecipientRecord | None:
        statement = (
            select(RecipientRow)
            .where(RecipientRow.user_id == user_id)
            .execution_options(populate_existing=True)
        )
        row = (await self._session.execute(statement)).scalar_one_or_none()
        return None if row is None else _to_record(row)

    async def lock_or_create(self, user_id: UUID) -> RecipientRecord:
        """The row of this user, created empty if absent, locked until commit.

        ``ON CONFLICT DO NOTHING`` first: an event about the account and a
        Telegram link can race for one user, and both must end with one row.
        The lock then keeps two of them from applying changes over each other.
        """
        await self._session.execute(
            insert(RecipientRow)
            .values(user_id=user_id)
            .on_conflict_do_nothing(index_elements=[RecipientRow.user_id])
        )
        statement = (
            select(RecipientRow)
            .where(RecipientRow.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        row = (await self._session.execute(statement)).scalar_one()
        return _to_record(row)

    async def forget_telegram_chat(self, user_id: UUID, *, chat_id: int | None = None) -> None:
        """Drop the user's chat; with ``chat_id``, only if it is still that chat.

        The condition matters when a delivery learns the chat is dead: the user
        may have linked a new one since the message was queued.
        """
        statement = update(RecipientRow).where(RecipientRow.user_id == user_id)
        if chat_id is not None:
            statement = statement.where(RecipientRow.telegram_chat_id == chat_id)
        await self._session.execute(statement.values(telegram_chat_id=None))

    async def save(self, record: RecipientRecord) -> None:
        """Write every field the scenarios may change."""
        await self._session.execute(
            update(RecipientRow)
            .where(RecipientRow.user_id == record.user_id)
            .values(
                email=record.email,
                phone=record.phone,
                email_confirmed=record.email_confirmed,
                telegram_chat_id=record.telegram_chat_id,
                is_active=record.is_active,
                preferences=record.preferences,
                contacts_updated_at=record.contacts_updated_at,
            )
        )
