"""The token booking presents when it calls another service.

Obtained from ``auth`` with the client credentials of this service and kept in
memory until shortly before it expires. Concurrent callers wait for one request
instead of each asking ``auth`` for a token of their own.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from pydantic import AwareDatetime, BaseModel, ConfigDict, SecretStr, ValidationError

from barber_common.http import ServiceClient
from barber_common.http.client import UpstreamError

__all__ = ["TOKEN_PATH", "ServiceTokenProvider"]

TOKEN_PATH = "/internal/v1/token"  # noqa: S105 - a path, not a credential

# Renewed this long before the expiry, so a token never runs out between the
# moment it is taken and the moment the other service checks it.
DEFAULT_REFRESH_MARGIN = timedelta(seconds=30)


class _TokenGrant(BaseModel):
    """The part of the answer of ``auth`` this client reads."""

    model_config = ConfigDict(extra="ignore")

    access_token: str
    expires_at: AwareDatetime


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ServiceTokenProvider:
    """A service token of this service, fetched lazily and reused."""

    def __init__(
        self,
        *,
        http: ServiceClient,
        client_id: str,
        client_secret: SecretStr,
        refresh_margin: timedelta = DEFAULT_REFRESH_MARGIN,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_margin = refresh_margin
        self._clock = clock
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at: datetime | None = None

    async def token(self) -> str:
        """A token good for at least the refresh margin."""
        current = self._current()
        if current is not None:
            return current

        async with self._lock:
            # Another caller may have fetched one while this one waited.
            current = self._current()
            if current is not None:
                return current
            return await self._fetch()

    def forget(self) -> None:
        """Drop the token, so the next call fetches a new one.

        For a token the other side refused before its expiry -- a key rotated
        in ``auth``, a clock that drifted.
        """
        self._token = None
        self._expires_at = None

    def _current(self) -> str | None:
        if self._token is None or self._expires_at is None:
            return None
        if self._clock() >= self._expires_at - self._refresh_margin:
            return None
        return self._token

    async def _fetch(self) -> str:
        response = await self._http.post(
            TOKEN_PATH,
            json={
                "client_id": self._client_id,
                "client_secret": self._client_secret.get_secret_value(),
            },
        )
        try:
            grant = _TokenGrant.model_validate_json(response.content)
        except ValidationError as error:
            raise UpstreamError(
                upstream=self._http.upstream,
                status_code=response.status_code,
                remote_code=None,
                detail="auth answered with something that is not a token",
            ) from error

        self._token = grant.access_token
        self._expires_at = grant.expires_at
        return grant.access_token
