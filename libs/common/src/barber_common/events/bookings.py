"""Payloads of ``booking.bookings.v1`` and ``booking.reminders.v1``.

Shared by ``booking``, which writes them, and ``notification``, which turns
them into messages. The partitioning key is the booking, so the events of one
appointment stay in order -- a cancellation must never overtake the creation it
cancels.

**No contact details travel here.** Who the client is reachable at belongs to
``auth``, and ``notification`` keeps its own table of recipients filled from
``auth.users.v1``. An event carries the data of its own aggregate and nothing
else.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict

__all__ = [
    "BOOKINGS_TOPIC",
    "BOOKING_AGGREGATE_TYPE",
    "REMINDERS_TOPIC",
    "BookingCancelled",
    "BookingCreated",
    "BookingEventType",
    "BookingRescheduled",
    "CancelledBy",
]

BOOKINGS_TOPIC = "booking.bookings.v1"
REMINDERS_TOPIC = "booking.reminders.v1"

# The partitioning key of every event here, and the aggregate they belong to.
BOOKING_AGGREGATE_TYPE = "bookings"


class BookingEventType(StrEnum):
    """``event_type`` of the envelope, for the producer and the consumer alike.

    The whole life cycle is listed, because the set of types on a topic is part
    of its contract; the payloads arrive with the tasks that publish them.
    """

    CREATED = "booking.created"
    RESCHEDULED = "booking.rescheduled"
    CANCELLED = "booking.cancelled"
    COMPLETED = "booking.completed"
    NO_SHOW = "booking.no_show"


class BookingCreated(BaseModel):
    """A client has an appointment.

    Everything a message about it needs, in the words of the booking itself:
    when, with whom, for what, and what it costs. ``service_name`` and ``price``
    are the snapshot the booking was made with, so a later change to the price
    list does not rewrite what the client was told.

    Frozen, because it describes something that has already happened, and
    tolerant of unknown fields, because a consumer running an older schema must
    not stop a partition over a field it does not know.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    booking_id: UUID
    salon_id: UUID
    master_id: UUID
    client_user_id: UUID

    service_id: UUID
    service_name: str
    price: Decimal
    currency: str

    start_at: AwareDatetime
    end_at: AwareDatetime
    status: str
    # When the reminder is due, or nothing if it is already in the past.
    reminder_at: datetime | None = None


class CancelledBy(StrEnum):
    """Which side called a visit off, which decides what the client is told."""

    CLIENT = "client"
    SALON = "salon"


class BookingCancelled(BaseModel):
    """A visit will not take place, and its time is free again.

    ``cancelled_by`` is the side, not the person: the client is told either
    that their cancellation went through or that the salon had to cancel.
    ``reason`` is whatever the canceller wrote, or nothing.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    booking_id: UUID
    salon_id: UUID
    master_id: UUID
    client_user_id: UUID

    service_name: str
    start_at: AwareDatetime
    end_at: AwareDatetime

    cancelled_by: CancelledBy
    reason: str | None = None


class BookingRescheduled(BaseModel):
    """The same visit, at another time.

    Both times travel, so the message can say "moved from ... to ..." without
    the recipient having kept the first one. ``reminder_at`` is due anew.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    booking_id: UUID
    salon_id: UUID
    master_id: UUID
    client_user_id: UUID

    service_name: str
    previous_start_at: AwareDatetime
    previous_end_at: AwareDatetime
    start_at: AwareDatetime
    end_at: AwareDatetime
    reminder_at: datetime | None = None
