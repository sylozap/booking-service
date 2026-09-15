"""Token verification: the signature, then the claims that must hold.

* Only RS256 is accepted; the algorithm is never taken from the token header.
* The issuer must match the configured one.
* Expiry allows a small leeway for clock skew between pods.
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

    One exception for every reason, so the caller cannot tell which check
    failed.
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

        Read before the signature is checked; it only selects which key is
        tried.
        """
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as error:
            raise InvalidToken("token header cannot be read") from error

        kid = header.get("kid")
        if not isinstance(kid, str):
            raise InvalidToken("token header does not name a key")
        return kid
