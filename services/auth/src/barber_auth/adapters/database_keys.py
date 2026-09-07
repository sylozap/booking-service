"""How ``auth`` verifies the tokens it issued itself.

Every other service fetches the public keys over HTTP from
``/.well-known/jwks.json`` and caches them. ``auth`` cannot: that endpoint is
its own, and a service calling itself over the network adds a hop and a startup
dependency on its own readiness for data it already has.

So it reads ``signing_keys`` directly. Two consequences worth stating.

**It is always current.** A key registered by another replica a second ago is
visible immediately, which matters during a rotation: while a rolling deploy is
half done, some pods sign with the new key and some with the old, and every pod
has to verify both. A snapshot taken at startup would not, and requests would
fail depending on which pod they landed on.

**There is no cache.** One indexed query against a table with one or two rows,
on the only endpoints of this service that verify a token at all -- role
management. Registration, login and refresh are anonymous and never come here.
A cache would trade that for the risk above, and the trade is not worth making.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from barber_auth.repositories.signing_keys import SigningKeyRepository
from barber_common.auth.keys import PublicKey, UnknownSigningKey, key_from_pem

__all__ = ["DatabaseKeys"]


class DatabaseKeys:
    """The active public keys, read from the table that holds them."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def key_for(self, kid: str) -> PublicKey:
        """The key under this name, or nothing if it is not active.

        A key an operator deactivated is not returned, which is how a retired
        key stops verifying tokens rather than merely stopping to sign them.
        """
        async with self._session_factory() as session:
            keys = await SigningKeyRepository(session).list_active()

        for key in keys:
            if key.kid == kid:
                return key_from_pem(key.public_pem)

        raise UnknownSigningKey(kid)
