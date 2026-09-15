"""Registering the signing key and publishing the public keys.

:class:`RegisterSigningKey` runs once at startup and stores the public key of
this process. :class:`PublishedKeys` answers JWKS requests with every active
key, which during a rotation is two.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from barber_auth.adapters.rsa_signer import public_jwk_of_pem
from barber_auth.domain.signing import PublicJwk, TokenSigner
from barber_auth.repositories.signing_keys import SigningKeyRepository
from barber_common.db.session import transaction
from barber_common.logging import get_logger

__all__ = ["PublishedKeys", "RegisterSigningKey"]

_logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class JsonWebKeySet:
    """The document ``/.well-known/jwks.json`` returns."""

    keys: tuple[PublicJwk, ...]


class RegisterSigningKey:
    """Store the public key this process signs with.

    Called from the lifespan before the first request is served. Idempotent:
    the ``kid`` is derived from the key, so every replica registers the same
    row.
    """

    def __init__(self, *, session: AsyncSession, signer: TokenSigner) -> None:
        self._session = session
        self._keys = SigningKeyRepository(session)
        self._signer = signer

    async def execute(self) -> str:
        """Publish the key and return its ``kid``."""
        async with transaction(self._session):
            await self._keys.register_active(
                kid=self._signer.kid,
                public_pem=self._signer.public_pem,
            )

        # The kid is public -- it travels in the header of every token -- so
        # unlike the key itself it belongs in the log.
        _logger.info("signing key registered", kid=self._signer.kid)
        return self._signer.kid


class PublishedKeys:
    """Read the active public keys in the form JWKS speaks."""

    def __init__(self, session: AsyncSession) -> None:
        self._keys = SigningKeyRepository(session)

    async def execute(self) -> JsonWebKeySet:
        """Every active key as a JWK, converted from the stored PEM."""
        keys = await self._keys.list_active()
        return JsonWebKeySet(
            keys=tuple(public_jwk_of_pem(key.public_pem, kid=key.kid) for key in keys)
        )
