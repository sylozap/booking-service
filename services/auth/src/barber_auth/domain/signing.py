"""The signing key port used by the scenarios.

A signer turns claims into a signed string, is named by a ``kid``, and exposes
its public key for JWKS. There is no ``verify``: tokens are verified by the
services that receive them.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["PublicJwk", "TokenSigner"]

# One entry of the ``keys`` array of a JWKS document, as defined by RFC 7517.
PublicJwk = dict[str, str]


@runtime_checkable
class TokenSigner(Protocol):
    """A key that signs access tokens and can show its public half."""

    @property
    def kid(self) -> str:
        """Stable name of this key, as it appears in the JWT header and in JWKS."""
        ...

    @property
    def public_pem(self) -> str:
        """The public half in PEM, which is what ``signing_keys`` stores."""
        ...

    def sign(self, claims: dict[str, object]) -> str:
        """Return the compact JWS of these claims, ``kid`` in the header."""
        ...
