"""The RS256 signing key of the service, behind the domain port.

The only module that knows about PEM, RSA and PyJWT. The private key comes from
the environment and stays in memory; the database holds only public keys.

The ``kid`` is the RFC 7638 thumbprint of the public JWK, so the same key always
has the same name and a restarted pod re-registers its existing row.
"""

from __future__ import annotations

import base64
import hashlib
import json

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey

from barber_auth.domain.signing import PublicJwk

__all__ = ["RsaTokenSigner", "public_jwk_of_pem", "thumbprint_of_pem"]

ALGORITHM = "RS256"
# RSA of this size or larger. Below it the signature is not worth making, and
# the check is here rather than in a code review because a key is generated
# once and lives for years.
MIN_KEY_SIZE_BITS = 2048


class InvalidSigningKey(ValueError):
    """The configured key is not an RSA private key this service can sign with.

    A startup misconfiguration, not a ``DomainError``.
    """


class RsaTokenSigner:
    """One RSA key pair, loaded once at startup and used for every token."""

    def __init__(self, private_key_pem: str) -> None:
        self._private_key = _load_private_key(private_key_pem)
        self._public_key = self._private_key.public_key()
        self._jwk = _public_jwk(self._public_key)
        self._kid = _thumbprint(self._jwk)

    @property
    def kid(self) -> str:
        """The RFC 7638 thumbprint naming this key."""
        return self._kid

    @property
    def public_pem(self) -> str:
        """The public half in PEM, as ``signing_keys.public_pem`` stores it."""
        return self._public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")

    def sign(self, claims: dict[str, object]) -> str:
        """Sign the claims, naming the key in the ``kid`` of the JOSE header."""
        return jwt.encode(
            claims,
            self._private_key,
            algorithm=ALGORITHM,
            headers={"kid": self._kid},
        )


def public_jwk_of_pem(public_pem: str, *, kid: str) -> PublicJwk:
    """Turn a stored public PEM back into the JWKS entry that describes it.

    This is the read path of the endpoint: ``signing_keys`` holds PEM because
    that is the portable form, and JWKS speaks JWK, so the conversion happens
    on the way out rather than being stored twice and allowed to disagree.
    """
    public_key = serialization.load_pem_public_key(public_pem.encode("ascii"))
    if not isinstance(public_key, RSAPublicKey):
        raise InvalidSigningKey("stored signing key is not an RSA public key")

    jwk = _public_jwk(public_key)
    # "sig" and RS256 are what a verifier needs in order to skip a key it
    # cannot use. They are outside the thumbprint on purpose -- RFC 7638
    # hashes only the members that define the key itself.
    return {**jwk, "kid": kid, "use": "sig", "alg": ALGORITHM}


def thumbprint_of_pem(public_pem: str) -> str:
    """The ``kid`` a stored public key would have. Used by tests and scripts."""
    public_key = serialization.load_pem_public_key(public_pem.encode("ascii"))
    if not isinstance(public_key, RSAPublicKey):
        raise InvalidSigningKey("stored signing key is not an RSA public key")
    return _thumbprint(_public_jwk(public_key))


def _load_private_key(private_key_pem: str) -> RSAPrivateKey:
    """Parse the configured PEM, refusing anything that cannot sign RS256."""
    try:
        key = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
    except (ValueError, TypeError) as error:
        # The message of the underlying library is kept: it distinguishes
        # "not a PEM" from "encrypted, needs a password", and neither quotes
        # the key material.
        raise InvalidSigningKey(f"signing key cannot be read: {error}") from error

    if not isinstance(key, RSAPrivateKey):
        raise InvalidSigningKey(f"signing key must be an RSA private key, got {type(key).__name__}")
    if key.key_size < MIN_KEY_SIZE_BITS:
        raise InvalidSigningKey(
            f"signing key must be at least {MIN_KEY_SIZE_BITS} bits, got {key.key_size}"
        )
    return key


def _public_jwk(public_key: RSAPublicKey) -> PublicJwk:
    """The three members RFC 7638 hashes for an RSA key: ``e``, ``kty``, ``n``.

    Returned in lexicographic order, so the canonical form is a plain
    ``json.dumps``.
    """
    numbers = public_key.public_numbers()
    return {
        "e": _b64url(numbers.e),
        "kty": "RSA",
        "n": _b64url(numbers.n),
    }


def _thumbprint(jwk: PublicJwk) -> str:
    """SHA-256 over the canonical JWK, base64url without padding (RFC 7638)."""
    canonical = json.dumps(jwk, separators=(",", ":"), sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(canonical).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _b64url(value: int) -> str:
    """Base64url of an unsigned integer, minimal length, no padding.

    The length is ``(bit_length + 7) // 8`` and not a fixed size: RFC 7518
    forbids leading zero octets, and a key whose modulus happens to start with
    one would otherwise get a thumbprint nobody else computes.
    """
    width = (value.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(value.to_bytes(width, "big")).rstrip(b"=").decode("ascii")
