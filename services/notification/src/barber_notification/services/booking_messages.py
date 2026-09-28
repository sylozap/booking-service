"""What a client is told about their booking: a template and its fields.

Pure functions of the event. Times are shown in the salon's zone, which the
event carries; an event without one -- published before the zone was part of
it -- is shown in UTC and says so, rather than in a zone that may be wrong.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from barber_common.events.bookings import (
    BookingCancelled,
    BookingCreated,
    BookingRescheduled,
    CancelledBy,
)

__all__ = [
    "TemplateAndFields",
    "cancelled_message",
    "created_message",
    "format_moment",
    "rescheduled_message",
]

TemplateAndFields = tuple[str, dict[str, object]]


def format_moment(moment: datetime, timezone: str | None) -> str:
    """``05.09.2026 15:00 (Europe/Moscow)``, or the same in UTC without a zone."""
    zone_name = timezone
    try:
        zone = ZoneInfo(timezone) if timezone else None
    except (ZoneInfoNotFoundError, ValueError):
        zone = None
    if zone is None:
        zone_name = "UTC"
        local = moment.astimezone(ZoneInfo("UTC"))
    else:
        local = moment.astimezone(zone)
    return f"{local:%d.%m.%Y %H:%M} ({zone_name})"


def created_message(event: BookingCreated) -> TemplateAndFields:
    return "booking_created", {
        "service_name": event.service_name,
        "start_at": format_moment(event.start_at, event.timezone),
    }


def rescheduled_message(event: BookingRescheduled) -> TemplateAndFields:
    return "booking_rescheduled", {
        "service_name": event.service_name,
        "previous_start_at": format_moment(event.previous_start_at, event.timezone),
        "start_at": format_moment(event.start_at, event.timezone),
    }


def cancelled_message(event: BookingCancelled) -> TemplateAndFields:
    """Two texts: the client's own cancellation, and one the salon made."""
    template = (
        "booking_cancelled_by_client"
        if event.cancelled_by is CancelledBy.CLIENT
        else "booking_cancelled_by_salon"
    )
    return template, {
        "service_name": event.service_name,
        "start_at": format_moment(event.start_at, event.timezone),
    }
