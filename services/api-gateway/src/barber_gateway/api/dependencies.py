"""What every request through the gateway passes before it goes anywhere.

One dependency on the proxy route and on the gateway's own endpoint, rather
than ASGI middleware: a refusal raised here is a ``DomainError``, and the error
handlers of the chassis turn it into the same ``problem+json`` as any other.
The probes, ``/metrics`` and the OpenAPI document are not behind it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from starlette.requests import Request

from barber_common.auth.dependencies import get_token_verifier
from barber_common.errors import Unauthorized
from barber_gateway.auth import Identity, identify, is_public

__all__ = ["Admitted", "admit"]


async def admit(request: Request) -> Identity:
    """Let the request through, or refuse it with a ``401``."""
    identity = await identify(request, get_token_verifier(request))

    if identity.token_rejected:
        raise Unauthorized("The access token is not valid")
    if not identity.is_authenticated and not is_public(request.method, request.url.path):
        raise Unauthorized("Authentication required")
    return identity


Admitted = Annotated[Identity, Depends(admit)]
