"""Public halves of the signing keys.

The table has exactly two queries: register the key this process signs with,
and list everything JWKS should publish. Nothing here ever sees a private key
-- that one lives in the signer and in the Secret it was loaded from.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from barber_auth.models.signing_key import SigningKey

__all__ = ["SigningKeyRepository"]


class SigningKeyRepository:
    """Access to ``signing_keys``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def register_active(self, *, kid: str, public_pem: str) -> None:
        """Record this key as active, doing nothing if it is already there.

        ``ON CONFLICT DO NOTHING`` rather than a read followed by an insert:
        several replicas start at the same time and hold the same key, so two
        of them race on the same ``kid``, and the loser of that race should
        carry on rather than crash the pod.

        A key that an operator deliberately deactivated stays deactivated: the
        conflict is not an update, so restarting a pod cannot silently bring a
        retired key back into JWKS.
        """
        statement = (
            insert(SigningKey)
            .values(kid=kid, public_pem=public_pem, is_active=True)
            .on_conflict_do_nothing(index_elements=[SigningKey.kid])
        )
        await self._session.execute(statement)

    async def list_active(self) -> Sequence[SigningKey]:
        """Every key JWKS publishes, oldest first.

        Oldest first so that a rotation appends rather than reorders: a
        consumer that caches the document and picks by ``kid`` does not care,
        and a human comparing two responses does.
        """
        statement = (
            select(SigningKey)
            .where(SigningKey.is_active)
            .order_by(SigningKey.created_at, SigningKey.kid)
        )
        result = await self._session.execute(statement)
        return result.scalars().all()
