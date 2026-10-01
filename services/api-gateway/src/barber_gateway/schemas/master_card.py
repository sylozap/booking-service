"""Body of ``GET /api/v1/masters/{id}/card``."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field

from barber_common.contracts.catalog import MasterCardResponse

__all__ = ["MasterCard", "SlotsStatus"]


class SlotsStatus(StrEnum):
    """Why the list of slots is what it is.

    Three values rather than a flag, because an empty list means three
    different things to the screen that draws it.
    """

    OK = "ok"
    UNAVAILABLE = "unavailable"
    NOT_REQUESTED = "not_requested"


class MasterCard(BaseModel):
    """Everything the screen of a master needs, in one answer."""

    master: MasterCardResponse = Field(description="The profile and the services, from catalog.")
    service_id: UUID | None = Field(description="The service the slots are for, if one was asked.")
    timezone: str | None = Field(
        description="The IANA zone of the salon; null when the slots were not read."
    )
    slots: list[datetime] = Field(description="The nearest free starts in UTC, in order.")
    slots_status: SlotsStatus = Field(
        description=(
            "`ok`: the slots are booking's answer, empty when nothing is free. "
            "`unavailable`: booking did not answer, the slots are unknown. "
            "`not_requested`: no `service_id` was given."
        )
    )
