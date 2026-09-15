"""``GET /.well-known/jwks.json``.

The path is defined by RFC 8615, so the router is mounted at the application
root rather than under ``/api/v1``. The endpoint is anonymous.
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

    During a key rotation both keys are listed, and a verifier picks one by the
    ``kid`` in the token header.
    """
    published = await scenario.execute()
    return JwksResponse(keys=list(published.keys))
