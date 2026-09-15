"""The signing key: its name, its public half, and what it must never leak."""

from __future__ import annotations

import base64

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from barber_auth.adapters.rsa_signer import (
    InvalidSigningKey,
    RsaTokenSigner,
    public_jwk_of_pem,
    thumbprint_of_pem,
)

# The RSA key and expected thumbprint published in RFC 7638, section 3.1.
RFC_7638_MODULUS = (
    "0vx7agoebGcQSuuPiLJXZptN9nndrQmbXEps2aiAFbWhM78LhWx4cbbfAAtVT86zwu1RK7aPFFxuhDR1L6tSoc_B"
    "JECPebWKRXjBZCiFV4n3oknjhMstn64tZ_2W-5JsGY4Hc5n9yBXArwl93lqt7_RN5w6Cf0h4QyQ5v-65YGjQR0_F"
    "DW2QvzqY368QQMicAtaSqzs8KJZgnYb9c7d0zgdAZHzu6qMQvRL5hajrn1n91CbOpbISD08qNLyrdkt-bFTWhAI4"
    "vMQFh6WeZu0fM4lFd2NcRwr3XPksINHaQ-G_xBniIqbw0Ls1jF44-csFCur-kEgU8awapJzKnqDKgw"
)
RFC_7638_EXPONENT = "AQAB"
RFC_7638_THUMBPRINT = "NzbLsXh8uDCcd-6MNwXF4W_7noWXFZAfHkxZsRGC9Xs"

# Every private component of an RSA JWK, by the member name RFC 7518 gives it.
# None of them may ever appear in what this service publishes.
PRIVATE_JWK_MEMBERS = ("d", "p", "q", "dp", "dq", "qi", "oth")


def _decode(value: str) -> int:
    return int.from_bytes(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)), "big")


@pytest.fixture
def rfc_7638_public_pem() -> str:
    """The public key of the worked example, in the form the table stores."""
    numbers = rsa.RSAPublicNumbers(_decode(RFC_7638_EXPONENT), _decode(RFC_7638_MODULUS))

    return (
        numbers.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )


def test_the_kid_is_the_thumbprint_the_rfc_publishes(rfc_7638_public_pem: str) -> None:
    computed = thumbprint_of_pem(rfc_7638_public_pem)

    assert computed == RFC_7638_THUMBPRINT


def test_the_same_key_always_gets_the_same_kid(private_key_pem: str) -> None:
    first = RsaTokenSigner(private_key_pem)
    second = RsaTokenSigner(private_key_pem)

    # This is what makes registering the key at startup idempotent: a restart
    # or a second replica writes the row it already owns.
    assert first.kid == second.kid


def test_two_keys_get_different_kids(private_key_pem: str, other_private_key_pem: str) -> None:
    one = RsaTokenSigner(private_key_pem)
    another = RsaTokenSigner(other_private_key_pem)

    assert one.kid != another.kid


def test_the_published_key_carries_no_private_component(private_key_pem: str) -> None:
    signer = RsaTokenSigner(private_key_pem)

    published = public_jwk_of_pem(signer.public_pem, kid=signer.kid)

    assert not set(published) & set(PRIVATE_JWK_MEMBERS)
    assert set(published) == {"kty", "n", "e", "kid", "use", "alg"}


def test_the_public_pem_is_a_public_key(private_key_pem: str) -> None:
    signer = RsaTokenSigner(private_key_pem)

    assert "PRIVATE KEY" not in signer.public_pem
    assert signer.public_pem.startswith("-----BEGIN PUBLIC KEY-----")


def test_a_signed_token_verifies_against_the_published_key(private_key_pem: str) -> None:
    signer = RsaTokenSigner(private_key_pem)
    published = public_jwk_of_pem(signer.public_pem, kid=signer.kid)

    token = signer.sign({"sub": "ivan"})

    # Through the JWK and not through the PEM: this is the path a consumer
    # takes, and it is what proves the modulus and exponent published in JWKS
    # describe the key that actually signed.
    key = jwt.PyJWK.from_dict({**published, "kty": "RSA"}).key
    assert jwt.decode(token, key, algorithms=["RS256"])["sub"] == "ivan"


def test_the_token_header_names_the_key(private_key_pem: str) -> None:
    signer = RsaTokenSigner(private_key_pem)

    token = signer.sign({"sub": "ivan"})

    # In the header, not the payload: a verifier has to pick the key before it
    # can check the signature over the payload.
    assert jwt.get_unverified_header(token)["kid"] == signer.kid
    assert jwt.get_unverified_header(token)["alg"] == "RS256"


def test_a_token_does_not_verify_against_another_key(
    private_key_pem: str, other_private_key_pem: str
) -> None:
    token = RsaTokenSigner(private_key_pem).sign({"sub": "ivan"})
    stranger = RsaTokenSigner(other_private_key_pem)

    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(
            token,
            jwt.PyJWK.from_dict(public_jwk_of_pem(stranger.public_pem, kid=stranger.kid)).key,
            algorithms=["RS256"],
        )


def test_a_key_that_is_not_a_pem_is_refused() -> None:
    with pytest.raises(InvalidSigningKey):
        RsaTokenSigner("not a key")


def test_a_key_below_the_minimum_size_is_refused() -> None:
    # noqa below: a breakable key is the subject of this test, not a mistake.
    weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)  # noqa: S505
    pem = weak.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")

    with pytest.raises(InvalidSigningKey, match="2048"):
        RsaTokenSigner(pem)


def test_a_key_of_the_wrong_kind_is_refused() -> None:
    pem = (
        ed25519.Ed25519PrivateKey.generate()
        .private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        .decode("ascii")
    )

    with pytest.raises(InvalidSigningKey, match="RSA"):
        RsaTokenSigner(pem)
