"""Settings of the catalog service."""

from __future__ import annotations

from pathlib import Path

from barber_common.config import BaseAppSettings

__all__ = ["ALEMBIC_INI", "CatalogSettings"]

# services/catalog/alembic.ini, two directories above the package. The startup
# check and the migration Job read the same file, so they cannot disagree about
# which revision is the head.
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


class CatalogSettings(BaseAppSettings):
    """Everything the chassis needs, plus what only this service has.

    Nothing of its own yet: the fields arrive with the stage that owns them.
    """

    service_name: str = "catalog"
