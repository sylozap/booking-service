"""The OpenAPI documents of the services, as the gateway collects them."""

from __future__ import annotations

from collections.abc import Mapping

from barber_common.http import ServiceClient, UpstreamError
from barber_gateway.routing import Upstream

__all__ = ["OpenApiClient"]


class OpenApiClient:
    """Reads ``/openapi.json`` of each service behind the gateway."""

    def __init__(self, *, http: Mapping[Upstream, ServiceClient]) -> None:
        self._http = dict(http)

    @property
    def upstreams(self) -> tuple[Upstream, ...]:
        return tuple(self._http)

    async def fetch(self, upstream: Upstream) -> dict[str, object]:
        """The document of one service, as it serves it."""
        client = self._http[upstream]
        response = await client.get("/openapi.json")
        try:
            document = response.json()
        except ValueError as error:
            raise _not_a_document(client, response.status_code) from error
        if not isinstance(document, dict):
            raise _not_a_document(client, response.status_code)
        return document


def _not_a_document(client: ServiceClient, status_code: int) -> UpstreamError:
    return UpstreamError(
        upstream=client.upstream,
        status_code=status_code,
        remote_code=None,
        detail=f"{client.upstream} answered /openapi.json with something that is not a document",
    )
