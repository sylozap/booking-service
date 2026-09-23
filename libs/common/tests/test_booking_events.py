"""The payload of booking.bookings.v1, as both sides of it see it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from barber_common.events.bookings import (
    BOOKING_AGGREGATE_TYPE,
    BOOKINGS_TOPIC,
    BookingCreated,
    BookingEventType,
)

START_AT = datetime(2026, 10, 5, 7, 0, tzinfo=UTC)


def booking_created(**overrides: object) -> BookingCreated:
    fields: dict[str, object] = {
        "booking_id": uuid4(),
        "salon_id": uuid4(),
        "master_id": uuid4(),
        "client_user_id": uuid4(),
        "service_id": uuid4(),
        "service_name": "Haircut",
        "price": Decimal("3500.00"),
        "currency": "RUB",
        "start_at": START_AT,
        "end_at": START_AT + timedelta(minutes=45),
        "status": "confirmed",
    }
    fields.update(overrides)
    return BookingCreated(**fields)


def test_the_topic_and_the_aggregate_are_the_ones_of_the_contract() -> None:
    assert BOOKINGS_TOPIC == "booking.bookings.v1"
    assert BOOKING_AGGREGATE_TYPE == "bookings"
    assert BookingEventType.CREATED.value == "booking.created"


def test_the_price_survives_the_trip_as_a_decimal() -> None:
    event = booking_created(price=Decimal("3500.55"))

    restored = BookingCreated.model_validate_json(event.model_dump_json())

    assert restored.price == Decimal("3500.55")


def test_a_naive_start_is_refused() -> None:
    with pytest.raises(ValidationError):
        booking_created(start_at=datetime(2026, 10, 5, 10, 0))  # noqa: DTZ001


def test_a_field_added_by_a_newer_producer_is_ignored() -> None:
    document = booking_created().model_dump(mode="json") | {"promotion_code": "SPRING"}

    event = BookingCreated.model_validate(document)

    assert event.service_name == "Haircut"


def test_the_payload_carries_no_contact_details() -> None:
    # Who the client is reachable at belongs to auth, and notification keeps
    # its own table of recipients.
    fields = set(BookingCreated.model_fields)

    assert not fields & {"email", "phone", "telegram_chat_id"}
