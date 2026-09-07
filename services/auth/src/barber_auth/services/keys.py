"""Registering the signing key and publishing the public ones.

Two scenarios that share a table and nothing else.

:class:`RegisterSigningKey` runs once, at startup: the process holds a private
key and the rest of the platform has to be able to find its public half.

:class:`PublishedKeys` runs on every JWKS request and answers with everything
currently active -- which during a rotation is two keys, and that is the point.
Tokens signed by the outgoing key keep verifying until they expire, so the
switch does not log the whole platform out (ADR-0010).
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
    """Announce the public half of the key this process signs with.

    Called from the lifespan before the first request is served: a token whose
    ``kid`` is not in JWKS is a token no service can verify, so the key has to
    be published before it is used.

    Idempotent by construction -- the ``kid`` is derived from the key material,
    so a restart, a second replica and a rolling deploy all register the same
    row and the second write does nothing.
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
        """Every active key as a JWK.

        The conversion from the stored PEM happens here rather than at write
        time: the table keeps one representation of a key, so there is no
        second one to drift out of step with it, and the modulus and exponent
        JWKS publishes are read out of the key itself on every request.
        """
        keys = await self._keys.list_active()
        return JsonWebKeySet(
            keys=tuple(public_jwk_of_pem(key.public_pem, kid=key.kid) for key in keys)
        )
