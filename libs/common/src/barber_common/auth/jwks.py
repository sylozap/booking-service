"""JWKS client that caches the public keys of ``auth`` and keeps them fresh.

* A failed refresh keeps the keys already loaded, so the service keeps working
  while ``auth`` is down.
* Keys are refreshed on a timer, and an unknown ``kid`` triggers an immediate
  fetch, so a key rotation needs no restart.
* Forced refreshes are rate limited and serialised, so a burst of tokens with
  an unknown ``kid`` costs one request to ``auth``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from barber_common.auth.keys import PublicKey, UnknownSigningKey, key_from_jwk
from barber_common.auth.verifier import TokenVerifier
from barber_common.config import BaseAppSettings, ConfigurationError
from barber_common.http.circuit_breaker import UpstreamUnavailable
from barber_common.http.client import ServiceClient, UpstreamError
from barber_common.logging import get_logger
from barber_common.metrics import gauge

__all__ = ["JWKS_PATH", "JwksClient", "jwks_verifier", "refreshing"]

_logger = get_logger(__name__)

# Well-known path defined by RFC 8615.
JWKS_PATH = "/.well-known/jwks.json"

# An age beyond the rotation window means the refresh has stopped and valid
# tokens are about to be rejected.
JWKS_CACHE_AGE = gauge(
    "jwks_cache_age_seconds",
    "Time since the signing keys were last fetched successfully",
)


class JwksClient:
    """Keys of ``auth``, cached in this process."""

    def __init__(
        self,
        *,
        base_url: str,
        cache_ttl_seconds: float = 300.0,
        refresh_min_interval_seconds: float = 10.0,
        client: ServiceClient | None = None,
    ) -> None:
        self._client = client or ServiceClient(base_url=base_url, upstream="auth")
        self._cache_ttl_seconds = cache_ttl_seconds
        self._refresh_min_interval_seconds = refresh_min_interval_seconds

        self._keys: dict[str, PublicKey] = {}
        self._fetched_at: float | None = None
        self._last_attempt_at: float | None = None
        # Serialises refreshes so a burst of requests for an unknown kid makes
        # one call rather than one call each.
        self._lock = asyncio.Lock()

    @property
    def cached_kids(self) -> frozenset[str]:
        """Which keys this process can verify against right now."""
        return frozenset(self._keys)

    async def key_for(self, kid: str) -> PublicKey:
        """The key named by a token header, fetching it if it is new.

        An unknown ``kid`` triggers one refresh, no more often than the minimum
        refresh interval.
        """
        key = self._keys.get(kid)
        if key is not None:
            return key

        if self._may_attempt_refresh():
            await self.refresh()
            key = self._keys.get(kid)
            if key is not None:
                return key

        raise UnknownSigningKey(kid)

    async def refresh(self) -> None:
        """Fetch the document, keeping the old keys if the fetch fails.

        Never raises. A caller cannot do anything useful with the failure --
        the alternative to stale keys is no keys -- so the failure is logged,
        the metrics show the cache ageing, and verification carries on.
        """
        async with self._lock:
            # Another task may have refreshed while this one waited for the
            # lock; the whole point of the lock is that it then does not fetch.
            if not self._may_attempt_refresh():
                return
            self._last_attempt_at = asyncio.get_running_loop().time()

            try:
                document = await self._fetch()
            except (UpstreamError, UpstreamUnavailable, httpx.HTTPError) as error:
                # WARNING and not ERROR: the cached keys still verify tokens,
                # so nothing is broken yet. What breaks is the next rotation,
                # and jwks_cache_age_seconds is what shows it coming.
                _logger.warning(
                    "jwks refresh failed, keeping cached keys",
                    cached_keys=len(self._keys),
                    error=str(error),
                )
            else:
                self._keys = document
                self._fetched_at = asyncio.get_running_loop().time()
                _logger.info("jwks refreshed", keys=len(self._keys))

            # On both paths: after a failure the age keeps growing, and that
            # growth is the signal worth alerting on.
            self._report()

    async def refresh_forever(self, stop: asyncio.Event) -> None:
        """Refresh on a timer until asked to stop.

        Started from the lifespan of the service. A timer as well as the
        on-demand fetch, so that a key withdrawn from JWKS stops being accepted
        within the TTL rather than for as long as the process happens to live.
        """
        _logger.info("jwks refresher started", ttl_seconds=self._cache_ttl_seconds)
        await self.refresh()
        while not stop.is_set():
            try:
                async with asyncio.timeout(self._cache_ttl_seconds):
                    await stop.wait()
            except TimeoutError:
                await self.refresh()
        _logger.info("jwks refresher stopped")

    async def aclose(self) -> None:
        """Release the HTTP connection pool."""
        await self._client.aclose()

    def _may_attempt_refresh(self) -> bool:
        """Whether enough time has passed since the last attempt."""
        if self._last_attempt_at is None:
            return True
        elapsed = asyncio.get_running_loop().time() - self._last_attempt_at
        return elapsed >= self._refresh_min_interval_seconds

    async def _fetch(self) -> dict[str, PublicKey]:
        """Read the document and turn it into keys by ``kid``."""
        response = await self._client.get(JWKS_PATH)
        payload = response.json()
        keys = payload.get("keys") if isinstance(payload, dict) else None
        if not isinstance(keys, list):
            raise ValueError("jwks document has no keys array")

        fetched: dict[str, PublicKey] = {}
        for entry in keys:
            if not isinstance(entry, dict):
                continue
            kid = entry.get("kid")
            if not isinstance(kid, str):
                continue
            try:
                fetched[kid] = key_from_jwk(entry)
            except ValueError as error:
                # One unusable key must not cost the service the others: a
                # document gaining a key of a type this version does not know
                # is a forward-compatible change, not an outage.
                _logger.warning("jwks entry skipped", kid=kid, error=str(error))
        return fetched

    def _report(self) -> None:
        """Publish how old the cached keys are."""
        if self._fetched_at is not None:
            age = asyncio.get_running_loop().time() - self._fetched_at
            JWKS_CACHE_AGE.set(age)


@asynccontextmanager
async def refreshing(client: JwksClient) -> AsyncIterator[JwksClient]:
    """Keep the keys of ``client`` fresh for as long as the block lasts.

    Used from the lifespan of a service. The task is kept in a local variable so
    it cannot be garbage collected mid-flight.
    """
    stop = asyncio.Event()
    task = asyncio.create_task(client.refresh_forever(stop), name="jwks-refresh")
    try:
        yield client
    finally:
        stop.set()
        await task
        await client.aclose()


def jwks_verifier(settings: BaseAppSettings) -> tuple[JwksClient, TokenVerifier]:
    """Build the pair a service needs to check the tokens it receives.

    Returns both because the lifespan has to do two things with them: keep the
    client refreshing, and hand the verifier to the dependencies::

        client, verifier = jwks_verifier(settings)
        async with refreshing(client):
            use_authentication(app, verifier)
            yield

    ``auth`` does not use this. It signs the tokens, so it already has the key
    and builds a verifier over ``StaticKeys`` instead of fetching from itself.
    """
    if settings.jwks_url is None:
        raise ConfigurationError(
            "JWKS_URL is not set: a service cannot verify the tokens it receives "
            "without the public keys of auth"
        )

    client = JwksClient(
        base_url=settings.jwks_url,
        cache_ttl_seconds=settings.jwks_cache_ttl_seconds,
        refresh_min_interval_seconds=settings.jwks_refresh_min_interval_seconds,
    )
    verifier = TokenVerifier(
        keys=client,
        issuer=settings.jwt_issuer,
        leeway_seconds=settings.jwt_leeway_seconds,
    )
    return client, verifier
