"""``POST /internal/v1/token``.

Internal endpoint, mounted at ``/internal/v1`` and not published through the
gateway. It requires no token: the client credentials in the body are what is
checked.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from barber_auth.api.v1.dependencies import IssueServiceTokenScenario
from barber_auth.schemas.service_tokens import ServiceTokenRequest, ServiceTokenResponse

__all__ = ["router"]

router = APIRouter(prefix="/internal/v1", tags=["internal"])


@router.post(
    "/token",
    status_code=status.HTTP_200_OK,
    summary="Exchange client credentials for a service token",
    responses={status.HTTP_401_UNAUTHORIZED: {"description": "Client id or secret is not correct"}},
)
async def issue_service_token(
    body: ServiceTokenRequest, scenario: IssueServiceTokenScenario
) -> ServiceTokenResponse:
    """Get a token for calls between services.

    An unknown client, a wrong secret and a deactivated client all answer `401`
    with the same body.

    The token carries `scopes`, no `roles`, and `typ` `service`. It is short
    lived and cannot be refreshed; the client requests a new one.
    """
    issued = await scenario.execute(
        client_id=body.client_id,
        client_secret=body.client_secret,
    )
    return ServiceTokenResponse(
        access_token=issued.access_token,
        expires_at=issued.expires_at,
        scopes=list(issued.scopes),
    )
