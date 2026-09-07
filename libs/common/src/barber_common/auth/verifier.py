"""Checking a token: the signature, then the claims that must hold.

One place decides what a valid token is, and every service shares it. The rules
are deliberately narrow.

**RS256 and nothing else.** The algorithm is fixed here rather than read from
the token header. A verifier that trusts the header accepts ``alg: none`` and
accepts a token signed with HMAC using the public key as the secret -- the two
oldest ways to forge a JWT.

**The issuer is checked.** Otherwise a token minted by the ``auth`` of the dev
cluster is accepted by production, which is the same signing key away from
being true.

**Expiry has a little leeway and no more.** Clocks between pods differ by
milliseconds and the leeway is there for that. It is not a grace period: an
access token that cannot be revoked is only acceptable because it expires
(ADR-0010).
"""

from __future__ import annotations

import jwt

from barber_common.auth.claims import Principal, principal_from_claims
from barber_common.auth.keys import UnknownSigningKey, VerificationKeys
from barber_common.logging import get_logger

__all__ = ["ALGORITHM", "InvalidToken", "TokenVerifier"]

_logger = get_logger(__name__)

ALGORITHM = "RS256"


class InvalidToken(Exception):
    """The token is not one this platform issued, or it no longer counts.

    Deliberately one exception for every reason -- bad signature, expired,
    wrong issuer, unknown key, malformed. The dependency turns all of them into
    the same ``401``: telling a caller which check failed is telling an
    attacker what to fix next.
    """


class TokenVerifier:
    """Verifies tokens against whatever key source it was given."""

    def __init__(
        self,
        *,
        keys: VerificationKeys,
        issuer: str,
        leeway_seconds: float = 5.0,
    ) -> None:
        self._keys = keys
        self._issuer = issuer
        self._leeway_seconds = leeway_seconds

    async def verify(self, token: str) -> Principal:
        """Check a compact JWS and return who it says is calling."""
        try:
            kid = self._kid_of(token)
            key = await self._keys.key_for(kid)
            claims = jwt.decode(
                token,
                key,
                algorithms=[ALGORITHM],
                issuer=self._issuer,
                leeway=self._leeway_seconds,
                options={
                    "require": ["exp", "iat", "iss", "sub"],
                    # No audience is set on the tokens of this platform, so
                    # there is nothing to check and asking would fail them all.
                    "verify_aud": False,
                },
            )
        except UnknownSigningKey as error:
            raise InvalidToken("token is signed by an unknown key") from error
        except jwt.InvalidTokenError as error:
            # The reason is logged and not returned. It is what an operator
            # needs and what an attacker would like.
            _logger.info("token rejected", reason=type(error).__name__)
            raise InvalidToken("token is not valid") from error

        try:
            return principal_from_claims(claims)
        except ValueError as error:
            raise InvalidToken("token does not identify a caller") from error

    def _kid_of(self, token: str) -> str:
        """The key name from the JOSE header.

        Read before the signature is checked, which is unavoidable -- the key
        has to be chosen before it can be used -- and safe, because the header
        decides nothing except which key is tried. A forged ``kid`` names a key
        that does not exist, or one that does not match the signature.
        """
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as error:
            raise InvalidToken("token header cannot be read") from error

        kid = header.get("kid")
        if not isinstance(kid, str):
            raise InvalidToken("token header does not name a key")
        return kid
