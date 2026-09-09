"""Bodies of ``/api/v1/services`` and ``/api/v1/salons/{id}/services``."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ServiceCreateRequest", "ServiceResponse", "ServiceUpdateRequest"]

# ISO 4217. Fixed length rather than a free string: a currency written three
# ways is three currencies as far as any sum over them is concerned.
CURRENCY_PATTERN = r"^[A-Z]{3}$"


class ServiceCreateRequest(BaseModel):
    """A new service in a salon's price list."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    base_duration_min: int = Field(
        gt=0,
        le=24 * 60,
        description="How long the service takes at the salon's standard pace.",
    )
    base_price: Decimal = Field(
        ge=0,
        max_digits=10,
        decimal_places=2,
        description="The salon's list price. A master may charge their own.",
    )
    currency: str = Field(pattern=CURRENCY_PATTERN, description="ISO 4217, for example `RUB`.")


class ServiceUpdateRequest(BaseModel):
    """A change to a service. Absent fields are left alone.

    ``is_archived`` is not here: withdrawing a service is
    ``POST /api/v1/services/{id}/archive``, which is one thing with one meaning
    rather than a flag that can be flipped back and forth by an ordinary edit.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    base_duration_min: int | None = Field(default=None, gt=0, le=24 * 60)
    base_price: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2)
    currency: str | None = Field(default=None, pattern=CURRENCY_PATTERN)


class ServiceResponse(BaseModel):
    """A service as the API shows it."""

    id: UUID
    salon_id: UUID
    name: str
    description: str | None
    base_duration_min: int
    base_price: Decimal
    currency: str
    is_archived: bool
    created_at: datetime
    updated_at: datetime
