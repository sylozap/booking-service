"""One-time email confirmation tokens."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from barber_auth.domain.identifiers import UserId
from barber_auth.models.email_confirmation import EmailConfirmation

__all__ = ["EmailConfirmationRepository"]


class EmailConfirmationRepository:
    """Access to ``email_confirmations``.

    Every method works with the hash of a token; the token itself never reaches
    this layer as a stored value.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        *,
        user_id: UserId,
        token_hash: str,
        expires_at: datetime,
    ) -> EmailConfirmation:
        """Stage a confirmation inside the caller's transaction."""
        confirmation = EmailConfirmation(
            user_id=user_id,
            token_hash=token_hash,
            expires_at=expires_at,
        )
        self._session.add(confirmation)
        await self._session.flush()
        return confirmation

    async def get_by_token_hash(self, token_hash: str) -> EmailConfirmation | None:
        """Find the confirmation a token belongs to.

        Returns the row whether or not it is still usable: whether it expired
        or was already used is a rule, and rules are decided in the domain, not
        by the absence of a row.
        """
        statement = select(EmailConfirmation).where(EmailConfirmation.token_hash == token_hash)
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def mark_used(
        self,
        *,
        confirmation: EmailConfirmation,
        used_at: datetime,
    ) -> None:
        """Spend the token. The row stays for the cleanup of T1.12 to prune."""
        confirmation.used_at = used_at
        await self._session.flush()
