"""The JWKS cache: rotation without a restart, and surviving auth being down."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from barber_common.auth.jwks import JWKS_PATH, JwksClient
from barber_common.auth.keys import UnknownSigningKey
from barber_common.http.client import RetryPolicy, ServiceClient

FIRST_KID = "key-one"
SECOND_KID = "key-two"


class FakeAuth:
    """A stand-in for the JWKS endpoint of ``auth``.

    Not a mock of our own code -- section 14 forbids that -- but a stub of the
    network boundary, wired in as an httpx transport so everything between the
    client and the socket is the real thing.
    """

    def __init__(self, kids: list[str]) -> None:
        self.kids = list(kids)
        self.requests = 0
        self.available = True

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if not self.available:
            raise httpx.ConnectError("auth is down", request=request)
        assert request.url.path == JWKS_PATH
        return httpx.Response(200, json={"keys": [_jwk(kid) for kid in self.kids]})


def _jwk(kid: str) -> dict[str, str]:
    """One published RSA key, in the form the endpoint serves."""
    import base64

    numbers = (
        _KEYS.setdefault(kid, rsa.generate_private_key(public_exponent=65537, key_size=2048))
        .public_key()
        .public_numbers()
    )

    def encode(value: int) -> str:
        width = (value.bit_length() + 7) // 8
        return base64.urlsafe_b64encode(value.to_bytes(width, "big")).rstrip(b"=").decode("ascii")

    return {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": encode(numbers.n),
        "e": encode(numbers.e),
    }


# Generated once per session: an RSA key costs a noticeable fraction of a
# second and none of these tests care which key it is.
_KEYS: dict[str, rsa.RSAPrivateKey] = {}


def build_client(auth: FakeAuth, *, refresh_min_interval_seconds: float = 0.0) -> JwksClient:
    """A client talking to the stub, with retries off so counts are readable.

    The rate limit is zero unless a test is about it: most of these are about
    the cache, and a test that has to wait ten seconds to observe a rotation is
    a test nobody runs.
    """
    return JwksClient(
        base_url="http://auth",
        client=ServiceClient(
            base_url="http://auth",
            upstream="auth",
            transport=auth.transport(),
            retry=RetryPolicy(attempts=1),
        ),
        refresh_min_interval_seconds=refresh_min_interval_seconds,
    )


async def test_a_published_key_is_returned(client_and_auth: tuple[JwksClient, FakeAuth]) -> None:
    client, _ = client_and_auth

    key = await client.key_for(FIRST_KID)

    assert key is not None


async def test_the_document_is_fetched_once_for_many_lookups(
    client_and_auth: tuple[JwksClient, FakeAuth],
) -> None:
    client, auth = client_and_auth

    await client.key_for(FIRST_KID)
    await client.key_for(FIRST_KID)
    await client.key_for(FIRST_KID)

    # The cache is the whole point: verification is on the hot path of every
    # authenticated request and must not become a call to auth.
    assert auth.requests == 1


async def test_a_rotation_is_picked_up_without_a_restart(
    client_and_auth: tuple[JwksClient, FakeAuth],
) -> None:
    client, auth = client_and_auth
    await client.key_for(FIRST_KID)

    auth.kids.append(SECOND_KID)
    key = await client.key_for(SECOND_KID)

    # An unknown kid means auth rotated a moment ago. Without this, deploying a
    # new signing key rejects every request until the TTL happens to elapse.
    assert key is not None


async def test_an_unknown_key_does_not_refetch_on_every_request() -> None:
    auth = FakeAuth([FIRST_KID])
    client = build_client(auth, refresh_min_interval_seconds=60.0)
    await client.key_for(FIRST_KID)
    fetches_before = auth.requests

    for _ in range(5):
        with pytest.raises(UnknownSigningKey):
            await client.key_for("forged-kid")

    # A burst of tokens naming a key that does not exist is what an attacker
    # produces. It costs nothing beyond the one refresh the interval allows.
    assert auth.requests == fetches_before


async def test_the_rate_limit_delays_a_rotation_by_at_most_the_interval() -> None:
    auth = FakeAuth([FIRST_KID])
    client = build_client(auth, refresh_min_interval_seconds=60.0)
    await client.key_for(FIRST_KID)

    auth.kids.append(SECOND_KID)

    # The price of the rate limit, stated rather than discovered: for up to one
    # interval after a fetch, a key registered in the meantime is not visible
    # and tokens signed with it answer 401. The interval is therefore the
    # length of the window a rotation is felt for, and is set in seconds.
    with pytest.raises(UnknownSigningKey):
        await client.key_for(SECOND_KID)


async def test_the_cache_survives_auth_being_unavailable(
    client_and_auth: tuple[JwksClient, FakeAuth],
) -> None:
    client, auth = client_and_auth
    await client.key_for(FIRST_KID)

    auth.available = False
    await client.refresh()

    # The service that issues tokens must not be a service every other one
    # dies with -- that coupling is what JWKS exists to avoid.
    assert await client.key_for(FIRST_KID) is not None


async def test_a_refresh_that_fails_raises_nothing(
    client_and_auth: tuple[JwksClient, FakeAuth],
) -> None:
    client, auth = client_and_auth
    auth.available = False

    await client.refresh()

    assert client.cached_kids == frozenset()


async def test_an_unusable_entry_does_not_cost_the_others() -> None:
    auth = FakeAuth([FIRST_KID])

    def with_a_stranger(request: httpx.Request) -> httpx.Response:
        payload = auth.handle(request).json()
        payload["keys"].append({"kty": "EC", "kid": "elliptic"})
        return httpx.Response(200, json=payload)

    client = JwksClient(
        base_url="http://auth",
        client=ServiceClient(
            base_url="http://auth",
            upstream="auth",
            transport=httpx.MockTransport(with_a_stranger),
            retry=RetryPolicy(attempts=1),
        ),
    )

    await client.refresh()

    # A document gaining a key of a type this version does not know is a
    # forward-compatible change, not an outage.
    assert client.cached_kids == frozenset({FIRST_KID})


async def test_a_concurrent_burst_makes_one_request() -> None:
    auth = FakeAuth([FIRST_KID])
    client = build_client(auth)

    await asyncio.gather(*(client.key_for(FIRST_KID) for _ in range(10)))

    # Without the lock, ten requests arriving at a cold cache would each fetch.
    assert auth.requests == 1


@pytest.fixture
def client_and_auth() -> tuple[JwksClient, FakeAuth]:
    auth = FakeAuth([FIRST_KID])
    return build_client(auth), auth
