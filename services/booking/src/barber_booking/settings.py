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

    # How long the answer to a repeated POST /api/v1/bookings is kept.
    idempotency_ttl_hours: int = 24

    # How long before the appointment the client is reminded. Written into the
    # booking when it is created, and recomputed on every reschedule.
    reminder_lead_hours: int = 4
    # How often the scheduler looks for due reminders, and how many bookings
    # one pass takes. A full batch is followed by the next one at once.
    reminder_scheduler_interval_seconds: float = 60.0
    reminder_scheduler_batch_size: int = 100

    # How often the time booked over the week ahead is summed for the business
    # dashboard. More often than the scrape tells Prometheus nothing new.
    booked_time_interval_seconds: float = 60.0

    # Availability is cached far shorter than the catalog: it changes whenever
    # anyone books, and a minute is the staleness a client can be shown.
    availability_cache_ttl_seconds: int = 60
