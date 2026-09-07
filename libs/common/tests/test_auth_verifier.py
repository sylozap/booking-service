"""What counts as a valid token, and what must never pass for one."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from barber_common.auth.claims import ACCESS_TOKEN_TYPE
from barber_common.auth.keys import StaticKeys
from barber_common.auth.verifier import InvalidToken, TokenVerifier

ISSUER = "https://barber.local/auth"
KID = "test-key"
OTHER_KID = "other-key"


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    """A key pair for the tests, generated rather than committed."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def stranger_key() -> rsa.RSAPrivateKey:
    """A key the platform does not know, for the forgery tests."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def public_pem(key: rsa.RSAPrivateKey) -> str:
    return (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )


@pytest.fixture
def verifier(signing_key: rsa.RSAPrivateKey) -> TokenVerifier:
    return TokenVerifier(
        keys=StaticKeys.from_pem(kid=KID, public_pem=public_pem(signing_key)),
        issuer=ISSUER,
    )


def token(
    key: rsa.RSAPrivateKey,
    *,
    kid: str = KID,
    algorithm: str = "RS256",
    **overrides: object,
) -> str:
    """A token the platform would issue, unless the test says otherwise."""
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "sub": str(uuid4()),
        "typ": ACCESS_TOKEN_TYPE,
        "roles": [{"role": "client", "salon_id": None}],
        "iss": ISSUER,
        "jti": str(uuid4()),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=15)).timestamp()),
    }
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm=algorithm, headers={"kid": kid})


async def test_a_token_of_this_platform_is_accepted(
    verifier: TokenVerifier, signing_key: rsa.RSAPrivateKey
) -> None:
    subject = str(uuid4())

    principal = await verifier.verify(token(signing_key, sub=subject))

    assert principal.subject == subject
    assert principal.token_type == ACCESS_TOKEN_TYPE


async def test_an_expired_token_is_refused(
    verifier: TokenVerifier, signing_key: rsa.RSAPrivateKey
) -> None:
    past = datetime.now(UTC) - timedelta(minutes=30)

    with pytest.raises(InvalidToken):
        await verifier.verify(
            token(
                signing_key,
                iat=int(past.timestamp()),
                exp=int((past + timedelta(minutes=15)).timestamp()),
            )
        )


async def test_a_token_signed_by_a_stranger_is_refused(
    verifier: TokenVerifier, stranger_key: rsa.RSAPrivateKey
) -> None:
    # Signed by another key but naming the kid of a real one: this is what a
    # forgery looks like, and the signature is what catches it.
    with pytest.raises(InvalidToken):
        await verifier.verify(token(stranger_key))


async def test_a_token_naming_an_unknown_key_is_refused(
    verifier: TokenVerifier, signing_key: rsa.RSAPrivateKey
) -> None:
    with pytest.raises(InvalidToken):
        await verifier.verify(token(signing_key, kid=OTHER_KID))


async def test_a_token_with_no_kid_is_refused(
    verifier: TokenVerifier, signing_key: rsa.RSAPrivateKey
) -> None:
    forged = jwt.encode({"sub": "ivan", "iss": ISSUER}, signing_key, algorithm="RS256")

    with pytest.raises(InvalidToken):
        await verifier.verify(forged)


async def test_a_token_from_another_environment_is_refused(
    verifier: TokenVerifier, signing_key: rsa.RSAPrivateKey
) -> None:
    # One signing key away from being a real risk: without this check a token
    # minted in dev is accepted in prod.
    with pytest.raises(InvalidToken):
        await verifier.verify(token(signing_key, iss="https://barber.dev/auth"))


async def test_an_unsigned_token_is_refused(verifier: TokenVerifier) -> None:
    forged = jwt.encode(
        {"sub": "ivan", "iss": ISSUER, "typ": ACCESS_TOKEN_TYPE},
        key="",
        algorithm="none",
        headers={"kid": KID},
    )

    # The oldest forgery there is: a verifier that reads the algorithm out of
    # the header accepts this.
    with pytest.raises(InvalidToken):
        await verifier.verify(forged)


async def test_a_token_signed_with_hmac_over_the_public_key_is_refused(
    verifier: TokenVerifier, signing_key: rsa.RSAPrivateKey
) -> None:
    # Assembled by hand rather than with jwt.encode, which refuses to make
    # this one. An attacker has no such scruples, and the point of the test is
    # what the verifier does with the result: the public key is public, so a
    # verifier that took HS256 from the header would let anyone mint tokens.
    header = _segment({"alg": "HS256", "typ": "JWT", "kid": KID})
    payload = _segment({"sub": "ivan", "iss": ISSUER, "typ": ACCESS_TOKEN_TYPE})
    signed = f"{header}.{payload}".encode("ascii")
    signature = hmac.new(public_pem(signing_key).encode("ascii"), signed, hashlib.sha256).digest()
    forged = f"{header}.{payload}.{_b64url(signature)}"

    with pytest.raises(InvalidToken):
        await verifier.verify(forged)


def _segment(value: dict[str, str]) -> str:
    return _b64url(json.dumps(value, separators=(",", ":")).encode("utf-8"))


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


@pytest.mark.parametrize("missing", ["exp", "iat", "iss", "sub"])
async def test_a_token_missing_a_required_claim_is_refused(
    verifier: TokenVerifier, signing_key: rsa.RSAPrivateKey, missing: str
) -> None:
    claims = {
        "sub": str(uuid4()),
        "typ": ACCESS_TOKEN_TYPE,
        "iss": ISSUER,
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int((datetime.now(UTC) + timedelta(minutes=15)).timestamp()),
    }
    del claims[missing]
    forged = jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": KID})

    with pytest.raises(InvalidToken):
        await verifier.verify(forged)


async def test_garbage_is_refused(verifier: TokenVerifier) -> None:
    with pytest.raises(InvalidToken):
        await verifier.verify("not-a-token")


async def test_clock_skew_within_the_leeway_is_tolerated(
    signing_key: rsa.RSAPrivateKey,
) -> None:
    verifier = TokenVerifier(
        keys=StaticKeys.from_pem(kid=KID, public_pem=public_pem(signing_key)),
        issuer=ISSUER,
        leeway_seconds=30,
    )
    just_expired = datetime.now(UTC) - timedelta(seconds=5)

    principal = await verifier.verify(
        token(
            signing_key,
            iat=int((just_expired - timedelta(minutes=15)).timestamp()),
            exp=int(just_expired.timestamp()),
        )
    )

    # Pods disagree about the time by milliseconds. The leeway is for that and
    # is not a grace period -- half a minute, not half an hour.
    assert principal.token_type == ACCESS_TOKEN_TYPE
