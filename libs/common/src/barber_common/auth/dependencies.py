"""FastAPI dependencies that close an endpoint with an authentication check.

::

    @router.post("/salons")
    async def create_salon(caller: Annotated[Principal, Depends(require_roles("super_admin"))]): ...

The service verifies the bearer token itself; gateway headers such as
``X-User-Id`` are never read. Roles are checked here, while the salon scope is
checked by the scenario with
:meth:`~barber_common.auth.claims.Principal.holds`.

The token is read through FastAPI's ``HTTPBearer``, so every closed endpoint
declares the bearer scheme in OpenAPI and Swagger UI offers to send a token.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.requests import Request

from barber_common.auth.claims import ACCESS_TOKEN_TYPE, SERVICE_TOKEN_TYPE, Principal
from barber_common.auth.verifier import InvalidToken, TokenVerifier
from barber_common.errors import Forbidden, Unauthorized
from barber_common.logging import get_logger

__all__ = [
    "AUTHORIZATION_HEADER",
    "CurrentPrincipal",
    "CurrentUser",
    "current_principal",
    "current_user",
    "get_token_verifier",
    "require_roles",
    "require_scopes",
    "require_service_token",
    "use_authentication",
]

_logger = get_logger(__name__)

AUTHORIZATION_HEADER = "Authorization"

# auto_error is off so a missing or malformed header is refused by this module
# with the platform's problem+json 401, not by FastAPI with its own body.
_bearer_scheme = HTTPBearer(
    scheme_name="BearerToken",
    description="An access token from `POST /api/v1/auth/login`.",
    auto_error=False,
)
BearerCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)]


def use_authentication(app: object, verifier: TokenVerifier) -> None:
    """Attach a verifier to the application, where the dependencies find it.

    Called from the lifespan of a service, next to ``use_database``. Typed
    loosely on purpose: importing ``FastAPI`` here would make every module that
    touches a claim depend on the web framework.
    """
    state = getattr(app, "state", None)
    if state is None:  # pragma: no cover - every FastAPI app has one
        raise RuntimeError("application has no state to attach the verifier to")
    state.token_verifier = verifier


def get_token_verifier(request: Request) -> TokenVerifier:
    """The verifier of the running application."""
    verifier = getattr(request.app.state, "token_verifier", None)
    if not isinstance(verifier, TokenVerifier):
        raise RuntimeError(
            "application state has no token verifier: "
            "call use_authentication in the lifespan before serving requests"
        )
    return verifier


async def current_principal(request: Request, credentials: BearerCredentials) -> Principal:
    """Whoever is calling, user or service, with the token already verified.

    Rarely used directly: an endpoint almost always wants
    :func:`current_user`, :func:`require_roles` or
    :func:`require_service_token`, all of which are built on this.
    """
    token = _bearer_token(credentials)
    verifier = get_token_verifier(request)
    try:
        return await verifier.verify(token)
    except InvalidToken as error:
        raise Unauthorized(str(error)) from error


VerifiedPrincipal = Annotated[Principal, Depends(current_principal)]


async def current_user(principal: VerifiedPrincipal) -> Principal:
    """The user behind the request, refusing a service token.

    A service token has no user behind it, and an endpoint that treats its
    ``sub`` as a user id writes rows owned by a client id.
    """
    if principal.token_type != ACCESS_TOKEN_TYPE:
        raise Unauthorized("a user token is required here")
    return principal


def require_roles(*roles: str) -> Callable[..., Awaitable[Principal]]:
    """Build a dependency that admits only these roles.

    Holding any one of them is enough: an endpoint open to both a
    ``salon_admin`` and a ``super_admin`` says so by naming both, and the
    caller needs one.
    """
    if not roles:
        raise ValueError("require_roles needs at least one role")
    allowed = frozenset(roles)

    async def dependency(principal: Annotated[Principal, Depends(current_user)]) -> Principal:
        if not principal.has_any_role(allowed):
            # INFO, not ERROR: a refused caller is normal traffic.
            _logger.info("access refused", subject=principal.subject)
            raise Forbidden("This operation is not allowed for your role")
        return principal

    return dependency


def require_service_token(*scopes: str) -> Callable[..., Awaitable[Principal]]:
    """Build a dependency for an endpoint under ``/internal``.

    Refuses a user token outright, whatever roles it carries: the endpoints
    behind this are not part of the public contract and a customer's token must
    never open one, however privileged the customer is.
    """
    required = frozenset(scopes)

    async def dependency(principal: VerifiedPrincipal) -> Principal:
        if principal.token_type != SERVICE_TOKEN_TYPE:
            raise Unauthorized("a service token is required here")
        if not required.issubset(principal.scopes):
            _logger.info("service call refused", subject=principal.subject)
            raise Forbidden("This service token does not carry the required scope")
        return principal

    return dependency


def require_scopes(*scopes: str) -> Callable[..., Awaitable[Principal]]:
    """Alias of :func:`require_service_token`, read at the call site.

    ``Depends(require_scopes("catalog:read"))`` says what is being demanded;
    ``require_service_token("catalog:read")`` says who may call. Same check.
    """
    return require_service_token(*scopes)


def _bearer_token(credentials: HTTPAuthorizationCredentials | None) -> str:
    """The token from the Authorization header, or a 401 when there is none.

    ``HTTPBearer`` returns ``None`` for a missing header or another scheme. The
    token is never logged and never put in an error message.
    """
    token = credentials.credentials.strip() if credentials is not None else ""
    if not token:
        raise Unauthorized("Authentication required")
    return token


# Ready-made annotations, so an endpoint does not repeat the Depends call.
# Role-checked endpoints build their own -- require_roles takes arguments and
# so has to be called at the point of use.
CurrentUser = Annotated[Principal, Depends(current_user)]
CurrentPrincipal = Annotated[Principal, Depends(current_principal)]
