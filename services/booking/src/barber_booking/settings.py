"""Settings of the booking service."""

from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr

from barber_common.config import BaseAppSettings

__all__ = ["ALEMBIC_INI", "BookingSettings"]

# services/booking/alembic.ini, two directories above the package. The startup
# check and the migration Job read the same file, so they cannot disagree about
# which revision is the head.
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


class BookingSettings(BaseAppSettings):
    """Everything the chassis needs, plus what only this service has."""

    service_name: str = "booking"

    # Base URLs of the services booking calls. No defaults: an address that
    # quietly points at localhost is a misconfiguration found in production.
    catalog_url: str
    auth_url: str

    # The client credentials booking exchanges for a service token in auth.
    service_client_id: str = "booking"
    service_client_secret: SecretStr
