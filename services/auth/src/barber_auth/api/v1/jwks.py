"""``GET /.well-known/jwks.json``.

**The path is not versioned and this module is.** ``/.well-known/`` is reserved
by RFC 8615 and its members are named by the specifications that define them,
so the endpoint cannot live under ``/api/v1`` the way the rest of the service
does; the module sits with the other v1 routers because that is where the API
of this service is assembled, and the router is mounted at the root of the
application rather than behind the ``/api/v1`` prefix.

The endpoint is anonymous, and has to be: the whole point is that any service,
and the gateway, can fetch the keys without holding a credential issued by the
service that publishes them.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from barber_auth.api.v1.dependencies import PublishedKeysScenario
from barber_auth.schemas.jwks import JwksResponse

__all__ = ["router"]

router = APIRouter(tags=["auth"])


@router.get(
    "/.well-known/jwks.json",
    status_code=status.HTTP_200_OK,
    summary="Public keys that verify the access tokens of the platform",
)
async def jwks(scenario: PublishedKeysScenario) -> JwksResponse:
    """Publish every active public key.

    During a key rotation this answers with two keys, and a verifier picks by
    the ``kid`` in the token header. That is what makes a rotation invisible to
    the tokens issued a minute before it: they keep verifying against the
    outgoing key until they expire (ADR-0010).

    Nothing private is served here, and nothing private exists in the table
    behind it.
    """
    published = await scenario.execute()
    return JwksResponse(keys=list(published.keys))
