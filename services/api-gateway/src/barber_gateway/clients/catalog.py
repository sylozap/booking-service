"""The card of a master, as the gateway reads it from ``catalog``.

The public ``GET /api/v1/masters/{id}``, without a token: the card is part of
the shop window, and catalog answers it to anyone. The answer is the shared
contract, so a change of its shape breaks the type check rather than a request.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import ValidationError

from barber_common.contracts.catalog import MasterCardResponse
from barber_common.errors import NotFound
from barber_common.http import ServiceClient, UpstreamError

__all__ = ["CatalogClient"]

_NOT_FOUND = 404


class CatalogClient:
    """Typed access to the public catalog endpoints the gateway reads."""

    def __init__(self, *, http: ServiceClient) -> None:
        self._http = http

    async def get_master_card(self, master_id: UUID) -> MasterCardResponse:
        """The profile of a master and what they offer.

        A master catalog does not know is ``404 not_found``. A catalog that
        does not answer is ``503``, one that answers with an error ``502``.
        """
        try:
            response = await self._http.get(f"/api/v1/masters/{master_id}")
        except UpstreamError as error:
            if error.status_code == _NOT_FOUND:
                raise NotFound("No such master") from error
            raise

        try:
            return MasterCardResponse.model_validate_json(response.content)
        except ValidationError as error:
            raise UpstreamError(
                upstream=self._http.upstream,
                status_code=response.status_code,
                remote_code=None,
                detail="catalog answered with something that is not the contract",
            ) from error
