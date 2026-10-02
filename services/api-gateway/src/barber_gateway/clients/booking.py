"""Free slots of a master, as the gateway reads them from ``booking``.

The public ``GET /api/v1/availability``, without a token. The one refusal of
booking that is the caller's doing -- the master does not offer the service
asked about -- comes back as the same ``422 service_not_offered``; any other
failure is booking's, and the caller decides what to make of it.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from pydantic import ValidationError

from barber_common.contracts.booking import AvailabilityResponse
from barber_common.errors import DomainError
from barber_common.http import ServiceClient, UpstreamError

__all__ = ["BookingClient", "ServiceNotOffered"]


class ServiceNotOffered(DomainError):
    """The master does not offer the service the slots were asked for."""

    code = "service_not_offered"
    http_status = 422
    title = "Master does not offer this service"


class BookingClient:
    """Typed access to the public booking endpoints the gateway reads."""

    def __init__(self, *, http: ServiceClient) -> None:
        self._http = http

    async def get_availability(
        self, *, master_id: UUID, service_id: UUID, date_from: date, date_to: date
    ) -> AvailabilityResponse:
        """Free starts of a master for a service, date by date."""
        try:
            response = await self._http.get(
                "/api/v1/availability",
                params={
                    "master_id": str(master_id),
                    "service_id": str(service_id),
                    "date_from": date_from.isoformat(),
                    "date_to": date_to.isoformat(),
                },
            )
        except UpstreamError as error:
            if error.remote_code == ServiceNotOffered.code:
                raise ServiceNotOffered("This master does not offer this service") from error
            raise

        try:
            return AvailabilityResponse.model_validate_json(response.content)
        except ValidationError as error:
            raise UpstreamError(
                upstream=self._http.upstream,
                status_code=response.status_code,
                remote_code=None,
                detail="booking answered with something that is not the contract",
            ) from error
