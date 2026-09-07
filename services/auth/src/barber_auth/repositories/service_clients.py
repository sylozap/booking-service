"""Machine-to-machine clients: one row per service that calls another."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from barber_auth.models.service_client import ServiceClient

__all__ = ["ServiceClientRepository"]


class ServiceClientRepository:
    """Access to ``service_clients``.

    The secret never reaches this layer as a plain value: what is stored and
    what is compared is a hash, exactly as for a password.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_active(self, client_id: str) -> ServiceClient | None:
        """One client, if it exists and has not been switched off.

        Inactive clients are filtered here rather than in the scenario: an
        operator disabling a client expects it to stop working, and a code path
        that reads the row and forgets the flag is how it keeps working.
        """
        statement = select(ServiceClient).where(
            ServiceClient.client_id == client_id,
            ServiceClient.is_active,
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def upsert(self, *, client_id: str, secret_hash: str, scopes: Sequence[str]) -> None:
        """Register a client from configuration, or update what it may do.

        Called at startup for every configured client. Unlike the signing key,
        a conflict here updates: the secret and the scopes come from the
        Secret of this deployment, so redeploying with a rotated secret has to
        take effect rather than be quietly ignored.

        ``is_active`` is deliberately not touched. A client an operator
        switched off stays off until they switch it back on, and a rolling
        restart must not undo that.
        """
        statement = insert(ServiceClient).values(
            client_id=client_id,
            secret_hash=secret_hash,
            scopes=list(scopes),
        )
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=[ServiceClient.client_id],
                set_={
                    "secret_hash": statement.excluded.secret_hash,
                    "scopes": statement.excluded.scopes,
                },
            )
        )
