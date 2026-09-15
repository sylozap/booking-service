"""Refresh tokens: issuing, locking, and revoking whole families.

Everything here works with the hash of a token. The token itself exists once,
in the response that carried it, and is never stored, logged or compared as a
plain value.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Update

from barber_auth.domain.identifiers import UserId
from barber_auth.models.refresh_token import RefreshToken

__all__ = ["RefreshTokenRepository"]


class RefreshTokenRepository:
    """Access to ``refresh_tokens``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        *,
        user_id: UserId,
        family_id: UUID,
        token_hash: str,
        expires_at: datetime,
        user_agent: str | None = None,
        ip: str | None = None,
    ) -> RefreshToken:
        """Stage a new token inside the caller's transaction."""
        token = RefreshToken(
            user_id=user_id,
            family_id=family_id,
            token_hash=token_hash,
            expires_at=expires_at,
            user_agent=user_agent,
            ip=ip,
        )
        self._session.add(token)
        await self._session.flush()
        return token

    async def lock_by_token_hash(self, token_hash: str) -> RefreshToken | None:
        """Find a token and lock its row until the transaction ends.

        ``FOR UPDATE`` serialises concurrent rotations of the same token: the
        second request reads the row as revoked, which is reuse. The row is
        returned whatever its state; the domain decides whether it is usable.
        """
        statement = (
            select(RefreshToken).where(RefreshToken.token_hash == token_hash).with_for_update()
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def get_by_token_hash(self, token_hash: str) -> RefreshToken | None:
        """Find a token without locking it. For logout, which revokes anyway."""
        statement = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def rotate(
        self,
        *,
        token: RefreshToken,
        replaced_by: UUID,
        revoked_at: datetime,
    ) -> None:
        """Retire a token in favour of the one that succeeds it.

        The row is kept, so a second use is detected as reuse, and
        ``replaced_by`` links it to its successor.
        """
        token.revoked_at = revoked_at
        token.replaced_by = replaced_by
        await self._session.flush()

    async def revoke_family(self, *, family_id: UUID, revoked_at: datetime) -> int:
        """Kill every live token of one family. Returns how many died.

        One statement rather than a loop: the rows are found and revoked in the
        same place, so a token inserted by a request that committed a moment
        ago cannot slip through between the read and the write.
        """
        statement = (
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=revoked_at)
        )
        return await self._execute_revocation(statement)

    async def revoke_all_of_user(self, *, user_id: UserId, revoked_at: datetime) -> int:
        """Log one user out of everywhere. Returns how many sessions ended.

        This is the query the index ``(user_id, revoked_at)`` exists for.
        """
        statement = (
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=revoked_at)
        )
        return await self._execute_revocation(statement)

    async def _execute_revocation(self, statement: Update) -> int:
        """Run a bulk revocation and report how many rows it touched.

        ``AsyncSession.execute`` is typed as returning a ``Result``, which has
        no ``rowcount`` -- an UPDATE really returns a ``CursorResult``, and the
        narrowing that says so is done once here instead of at both call sites.
        """
        result = await self._session.execute(statement)
        if not isinstance(result, CursorResult):  # pragma: no cover - UPDATE always is one
            raise TypeError("expected a cursor result from an UPDATE statement")
        return result.rowcount
