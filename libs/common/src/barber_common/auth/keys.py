"""Where a service gets the public keys it verifies tokens with.

Two implementations answer the same question, "give me the key named by this
``kid``", and the difference is what happens when the answer is not known yet.

:class:`StaticKeys` holds keys it was given. ``auth`` uses it: the service that
signs the tokens already has the key, and fetching it over HTTP from itself
would add a network call and a startup dependency on its own readiness.

:class:`~barber_common.auth.jwks.JwksClient` fetches them from ``auth`` and
caches them. Every other service uses that.

The verifier depends on this port and not on either of them, so the rules --
which algorithm, which issuer, what counts as expired -- are written once and
do not care where the key came from.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from jwt import PyJWK

__all__ = [
    "PublicKey",
    "StaticKeys",
    "UnknownSigningKey",
    "VerificationKeys",
    "key_from_jwk",
    "key_from_pem",
]

# What PyJWT is handed to check a signature.
PublicKey = RSAPublicKey


class UnknownSigningKey(LookupError):
    """No key is published under this ``kid``.

    Not a domain error: the dependency that catches it decides what a caller
    should be told, and the answer is the same ``401`` an invalid signature
    gets. Distinguishing them for the caller would say whether a token was
    signed by a key this platform once had.
    """


@runtime_checkable
class VerificationKeys(Protocol):
    """The public keys of the platform, by the name a token header carries."""

    async def key_for(self, kid: str) -> PublicKey:
        """Return the key, fetching it if this source can. Raises if unknown."""
        ...


class StaticKeys:
    """A fixed set of keys, for the service that owns them.

    Used by ``auth``: it holds the private key, so it also holds the public
    half, and an unknown ``kid`` here is genuinely unknown rather than merely
    not fetched yet.
    """

    def __init__(self, keys: dict[str, PublicKey]) -> None:
        self._keys = dict(keys)

    @classmethod
    def from_pem(cls, *, kid: str, public_pem: str) -> StaticKeys:
        """Build a source holding one key, given its stored PEM."""
        return cls({kid: key_from_pem(public_pem)})

    def add(self, *, kid: str, key: PublicKey) -> None:
        """Publish one more key, as a rotation does."""
        self._keys[kid] = key

    async def key_for(self, kid: str) -> PublicKey:
        """The key under this name."""
        try:
            return self._keys[kid]
        except KeyError as error:
            raise UnknownSigningKey(kid) from error


def key_from_pem(public_pem: str) -> PublicKey:
    """Turn a stored public PEM into a key object.

    The form ``signing_keys`` keeps, and therefore the form ``auth`` verifies
    from. Anything that is not an RSA public key is refused: the platform signs
    with RS256 and a key of another kind means the row is not what it claims.
    """
    key = load_pem_public_key(public_pem.encode("ascii"))
    if not isinstance(key, RSAPublicKey):
        raise ValueError("verification key is not an RSA public key")
    return key


def key_from_jwk(jwk: dict[str, str]) -> PublicKey:
    """Turn one entry of a JWKS document into a key object.

    Anything that is not an RSA key usable for signatures is refused rather
    than skipped silently: the platform issues RS256 and nothing else, so a
    different key in the document means the document is not the one this code
    expects.
    """
    if jwk.get("kty") != "RSA":
        raise ValueError(f"unsupported key type {jwk.get('kty')!r}")
    if jwk.get("use") not in (None, "sig"):
        raise ValueError(f"key is not for signatures: use={jwk.get('use')!r}")

    # PyJWK does the base64url and the modulus arithmetic. Going through it
    # rather than reimplementing that here means one library owns the format.
    key = PyJWK.from_dict(dict(jwk)).key
    if not isinstance(key, RSAPublicKey):
        raise ValueError("published key is not an RSA public key")
    return key
