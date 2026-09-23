"""The settings of a master refuse what they could not work with."""

from __future__ import annotations

from uuid import uuid4

import pytest

from barber_booking.domain.identifiers import MasterId, SalonId, UserId
from barber_booking.domain.master import MasterSettings


def settings(*, timezone: str = "Europe/Moscow", buffer_after_min: int = 0) -> MasterSettings:
    return MasterSettings(
        master_id=MasterId(uuid4()),
        salon_id=SalonId(uuid4()),
        user_id=UserId(uuid4()),
        timezone=timezone,
        buffer_after_min=buffer_after_min,
        is_active=True,
    )


def test_a_known_zone_is_accepted() -> None:
    assert settings(timezone="Asia/Yekaterinburg").zone.key == "Asia/Yekaterinburg"


def test_a_zone_that_does_not_exist_is_refused() -> None:
    with pytest.raises(ValueError, match="IANA"):
        settings(timezone="Mars/Olympus_Mons")


def test_a_negative_buffer_is_refused() -> None:
    with pytest.raises(ValueError, match="buffer"):
        settings(buffer_after_min=-5)
