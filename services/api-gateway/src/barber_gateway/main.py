"""Entry point of the api-gateway service.

Single entry point: it verifies the JWT, limits the rate, stamps the
correlation id and proxies to the services behind it.
"""

from __future__ import annotations

from fastapi import FastAPI

from barber_common.app import create_app
from barber_gateway.api.v1.router import router
from barber_gateway.settings import GatewaySettings

__all__ = ["create_application"]


def create_application(settings: GatewaySettings | None = None) -> FastAPI:
    """Build the application. Called by uvicorn with ``--factory``.

    ``settings`` is an argument so a test can assemble the service without an
    environment behind it.
    """
    return create_app(
        settings or GatewaySettings.load(),
        routers=[router],
        title="Barber API Gateway",
    )
