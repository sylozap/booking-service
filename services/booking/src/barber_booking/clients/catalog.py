"""The internal endpoint of ``catalog``, as booking calls it.

One call answers everything availability and a new booking need about one
master offering one service. The answer is the shared contract, so a change of
its shape breaks the type check here rather than a request at runtime.

Failures come back in domain terms. A ``404`` means there is nothing to book
and becomes ``service_not_offered``; a catalog that does not answer, or a breaker
that is open, is ``503 upstream_unavailable``. A deactivated master is not a
failure: it arrives as ``master_active`` false, and the caller decides what that
means for it.
"""

from __future__ import annotations

from pydantic import ValidationError

from barber_booking.clients.service_token import ServiceTokenProvider
from barber_booking.domain.errors import ServiceNotOffered
from barber_booking.domain.identifiers import MasterId, ServiceId
from barber_common.contracts.catalog import MasterServiceDetails
from barber_common.http import ServiceClient
from barber_common.http.client import UpstreamError

__all__ = ["CatalogClient"]

_NOT_FOUND = 404
_UNAUTHORIZED = 401


class CatalogClient:
    """Typed access to ``GET /internal/v1/masters/{id}/services/{sid}``."""

    def __init__(self, *, http: ServiceClient, tokens: ServiceTokenProvider) -> None:
        self._http = http
        self._tokens = tokens

    async def get_offering(
        self, *, master_id: MasterId, service_id: ServiceId
    ) -> MasterServiceDetails:
        """What this master charges for this service, and the salon's policies."""
        try:
            return await self._request(master_id, service_id)
        except UpstreamError as error:
            if error.upstream != self._http.upstream or error.status_code != _UNAUTHORIZED:
                raise
            # A token refused before its expiry: fetched again, tried once more.
            self._tokens.forget()
            return await self._request(master_id, service_id)

    async def _request(self, master_id: MasterId, service_id: ServiceId) -> MasterServiceDetails:
        token = await self._tokens.token()
        try:
            response = await self._http.get(
                f"/internal/v1/masters/{master_id}/services/{service_id}",
                service_token=token,
            )
        except UpstreamError as error:
            if error.status_code == _NOT_FOUND:
                raise ServiceNotOffered("This master does not offer this service") from error
            raise

        try:
            return MasterServiceDetails.model_validate_json(response.content)
        except ValidationError as error:
            raise UpstreamError(
                upstream=self._http.upstream,
                status_code=response.status_code,
                remote_code=None,
                detail="catalog answered with something that is not the contract",
            ) from error
