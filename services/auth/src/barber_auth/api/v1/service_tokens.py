"""``POST /internal/v1/token``.

**The path is not under ``/api/v1`` and this module is under ``api/v1``.**
Internal endpoints are not part of the public contract and are not published
through the gateway (docs/04-api-contracts.md); the module sits with the other
routers because that is where the API of this service is assembled, and the
router is mounted at ``/internal/v1`` rather than behind the public prefix.

The endpoint is anonymous in the sense that it demands no token -- the client
credentials in the body *are* the credentials being checked. It is the one
place a service proves who it is, and everything it can reach afterwards is
closed with ``require_service_token``.
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

    **An unknown client and a wrong secret answer identically**, with `401` and
    the same body, and take comparable time to do it: otherwise the endpoint
    lists the services of the platform to anyone who asks. A client an operator
    switched off answers the same way.

    The token carries `scopes` and no `roles`, and its `typ` is `service`. That
    claim is what lets an endpoint under `/internal` refuse a customer's token
    by reading one field rather than guessing from the shape of the subject --
    including a customer who happens to be a `super_admin`.

    It is short lived and there is no refresh: the client holds its credentials
    permanently and asks again.
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
