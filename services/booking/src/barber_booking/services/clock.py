"""The current moment, as something a scenario can be handed."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

__all__ = ["Clock", "utc_now"]

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)
