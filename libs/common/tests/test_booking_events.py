"""The payload of booking.bookings.v1, as both sides of it see it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import BaseModel, ValidationError

from barber_common.events.bookings import (
    BOOKING_AGGREGATE_TYPE,
    BOOKINGS_TOPIC,
    BookingCancelled,
    BookingCompleted,
    BookingCreated,
    BookingEventType,
    BookingNoShow,
    BookingRescheduled,
    CancelledBy,
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


@pytest.mark.parametrize(
    "payload",
    [BookingCreated, BookingCancelled, BookingRescheduled, BookingCompleted, BookingNoShow],
)
def test_the_payload_carries_no_contact_details(payload: type[BaseModel]) -> None:
    # Who the client is reachable at belongs to auth, and notification keeps
    # its own table of recipients.
    fields = set(payload.model_fields)

    assert not fields & {"email", "phone", "telegram_chat_id"}


def test_a_cancellation_says_which_side_called_it_off() -> None:
    event = BookingCancelled(
        booking_id=uuid4(),
        salon_id=uuid4(),
        master_id=uuid4(),
        client_user_id=uuid4(),
        service_name="Haircut",
        start_at=START_AT,
        end_at=START_AT + timedelta(minutes=45),
        cancelled_by=CancelledBy.SALON,
        reason="master_deactivated",
    )

    document = event.model_dump(mode="json")

    assert document["cancelled_by"] == "salon"
    assert document["reason"] == "master_deactivated"


def test_a_move_carries_both_the_old_time_and_the_new_one() -> None:
    later = START_AT + timedelta(days=1)
    event = BookingRescheduled(
        booking_id=uuid4(),
        salon_id=uuid4(),
        master_id=uuid4(),
        client_user_id=uuid4(),
        service_name="Haircut",
        previous_start_at=START_AT,
        previous_end_at=START_AT + timedelta(minutes=45),
        start_at=later,
        end_at=later + timedelta(minutes=45),
    )

    restored = BookingRescheduled.model_validate_json(event.model_dump_json())

    assert (restored.previous_start_at, restored.start_at) == (START_AT, later)


def test_the_zone_of_the_salon_is_optional_and_survives_the_trip() -> None:
    without = booking_created()
    with_zone = booking_created(timezone="Asia/Yekaterinburg")

    assert without.timezone is None
    assert BookingCreated.model_validate_json(with_zone.model_dump_json()) == with_zone


@pytest.mark.parametrize("payload", [BookingCompleted, BookingNoShow])
def test_a_closed_visit_is_described_by_its_booking(
    payload: type[BookingCompleted] | type[BookingNoShow],
) -> None:
    event = payload(
        booking_id=uuid4(),
        salon_id=uuid4(),
        master_id=uuid4(),
        client_user_id=uuid4(),
        service_name="Haircut",
        start_at=START_AT,
        end_at=START_AT + timedelta(minutes=45),
    )

    assert payload.model_validate_json(event.model_dump_json()) == event
