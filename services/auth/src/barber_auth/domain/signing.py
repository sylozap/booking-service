"""What the scenarios need from a signing key, and nothing more.

The domain does not know RSA, PEM or PyJWT. It knows that something can turn a
set of claims into a signed string, that the something has a ``kid`` naming it,
and that its public half can be published. The implementation is an adapter
(docs/CODING_STANDARDS.md sections 2.2 and 4).

The port has no ``verify``. Verification of an access token happens in the
service that receives it, against the public key it fetched from JWKS -- never
here (ADR-0010). ``auth`` only signs.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["PublicJwk", "TokenSigner"]

# One entry of the ``keys`` array of a JWKS document, already in the shape RFC
# 7517 gives it: a mapping of string to string. Typed as a plain dict because
# nothing in the domain inspects the members -- the repository stores the PEM
# and the endpoint serialises the mapping.
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
