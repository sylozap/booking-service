"""Bodies of ``POST /internal/v1/token``."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ServiceTokenRequest", "ServiceTokenResponse"]


class ServiceTokenRequest(BaseModel):
    """Client credentials of one service.

    A JSON body rather than the form encoding of the OAuth2 client credentials
    grant: the platform has no external OAuth consumer to be compatible with,
    and one way of reading a body across every endpoint is worth more here than
    conformance to a flow nobody else speaks.
    """

    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=1, max_length=64)
    client_secret: str = Field(min_length=1, max_length=256)


class ServiceTokenResponse(BaseModel):
    """The token an internal caller presents on its next call.

    No refresh token: the client holds its credentials permanently and asks
    again when this expires.
    """

    access_token: str
    token_type: str = "Bearer"  # noqa: S105 - the scheme name of RFC 6750
    expires_at: datetime
    scopes: list[str]
