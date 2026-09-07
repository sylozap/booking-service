"""Machine-to-machine tokens: registering clients, and issuing to them.

A service calling another service is still a caller and still has to prove who
it is. It does so with a client id and a secret, and gets back a short-lived
token carrying scopes instead of roles.

The secret is treated exactly as a password: argon2, never stored in the clear,
never logged, and a missing client costs the same time as a wrong secret so
that the endpoint cannot be used to find out which services exist.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from barber_auth.domain.errors import InvalidCredentials
from barber_auth.domain.passwords import PasswordHasher
from barber_auth.domain.signing import TokenSigner
from barber_auth.domain.tokens import build_service_claims
from barber_auth.models.service_client import ServiceClient
from barber_auth.repositories.service_clients import ServiceClientRepository
from barber_auth.settings import ServiceClientConfig
from barber_common.db.session import transaction
from barber_common.logging import get_logger

__all__ = ["IssueServiceToken", "RegisterServiceClients", "ServiceToken"]

_logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ServiceToken:
    """What an internal caller gets back.

    No refresh token: a service asks for a new one when this expires, which it
    can do at any moment because it holds its credentials permanently. A
    rotation mechanism would exist to protect a secret that never travels.
    """

    access_token: str
    expires_at: datetime
    scopes: tuple[str, ...]


class RegisterServiceClients:
    """Put the configured clients into the database at startup.

    Configuration and not a migration: a client seeded by a migration carries
    its secret hash in git, and rotating it becomes a schema change. Here the
    secret comes from the Secret of the deployment and only the hash is stored.
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        hasher: PasswordHasher,
        clients: dict[str, ServiceClientConfig],
    ) -> None:
        self._session = session
        self._clients = ServiceClientRepository(session)
        self._hasher = hasher
        self._configured = clients

    async def execute(self) -> int:
        """Register every configured client. Returns how many there were."""
        if not self._configured:
            return 0

        async with transaction(self._session):
            for client_id, config in self._configured.items():
                # argon2 in a thread, as everywhere else: this runs at startup
                # and would otherwise block the event loop for as many tens of
                # milliseconds as there are clients.
                secret_hash = await asyncio.to_thread(
                    self._hasher.hash, config.secret.get_secret_value()
                )
                await self._clients.upsert(
                    client_id=client_id,
                    secret_hash=secret_hash,
                    scopes=config.scopes,
                )

        # The ids and the scopes are not secret; the secrets are, and are not
        # here.
        _logger.info("service clients registered", clients=sorted(self._configured))
        return len(self._configured)


class IssueServiceToken:
    """Exchange client credentials for a short-lived service token."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        hasher: PasswordHasher,
        signer: TokenSigner,
        issuer: str,
        ttl_minutes: int,
    ) -> None:
        self._session = session
        self._clients = ServiceClientRepository(session)
        self._hasher = hasher
        self._signer = signer
        self._issuer = issuer
        self._ttl_minutes = ttl_minutes

    async def execute(
        self,
        *,
        client_id: str,
        client_secret: str,
        now: datetime | None = None,
    ) -> ServiceToken:
        """Issue one token to one client."""
        issued_at = now or datetime.now(UTC)

        # A transaction of its own around the read, closed before the hash is
        # verified: a query opens one whether or not it is asked for, and
        # argon2 must not be spent holding a connection. The same shape as the
        # login scenario, and for the same two reasons.
        async with transaction(self._session):
            client = await self._clients.get_active(client_id)

        if not await self._secret_matches(client=client, secret=client_secret):
            raise InvalidCredentials("Client id or secret is not correct")

        if client is None:  # pragma: no cover - _secret_matches rejects a missing client
            raise InvalidCredentials("Client id or secret is not correct")

        scopes = tuple(client.scopes)
        claims = build_service_claims(
            client_id=client.client_id,
            scopes=scopes,
            issuer=self._issuer,
            token_id=uuid4(),
            issued_at=issued_at,
            ttl_minutes=self._ttl_minutes,
        )

        _logger.info("service token issued", client_id=client.client_id)
        return ServiceToken(
            access_token=self._signer.sign(claims),
            expires_at=issued_at + timedelta(minutes=self._ttl_minutes),
            scopes=scopes,
        )

    async def _secret_matches(self, *, client: ServiceClient | None, secret: str) -> bool:
        """Verify, spending the same time whether or not the client exists.

        Without the dummy verification an unknown client id answers in
        microseconds and a wrong secret in the cost of an argon2 pass, and the
        difference lists the services of the platform to anyone who asks.
        """
        if client is None:
            await asyncio.to_thread(self._hasher.verify_dummy)
            return False
        return await asyncio.to_thread(self._hasher.verify, secret, client.secret_hash)
