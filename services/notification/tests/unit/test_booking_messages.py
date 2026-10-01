"""What a client is told, and on which channels -- without a database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from barber_common.events.bookings import (
    BookingCancelled,
    BookingCreated,
    BookingRescheduled,
    CancelledBy,
    ReminderDue,
)
from barber_common.events.users import UserEmailConfirmationRequested
from barber_notification.preferences import NotificationKind
from barber_notification.providers.base import Channel
from barber_notification.rendering import render
from barber_notification.repositories.recipients import RecipientRecord
from barber_notification.services.account_messages import confirmation_message
from barber_notification.services.booking_messages import (
    cancelled_message,
    created_message,
    format_moment,
    reminder_message,
    rescheduled_message,
)
from barber_notification.services.dispatch import reachable_channels

NOON_UTC = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


BOOKINGS = NotificationKind.BOOKINGS
REMINDERS = NotificationKind.REMINDERS


def recipient(**overrides: object) -> RecipientRecord:
    fields: dict[str, object] = {
        "user_id": uuid4(),
        "email": "client@example.com",
        "phone": None,
        "email_confirmed": True,
        "telegram_chat_id": 42,
        "is_active": True,
        "preferences": {},
        "contacts_updated_at": None,
    }
    fields.update(overrides)
    return RecipientRecord(**fields)  # type: ignore[arg-type]  # fields are typed per key


def cancellation(by: CancelledBy) -> BookingCancelled:
    return BookingCancelled(
        booking_id=uuid4(),
        salon_id=uuid4(),
        master_id=uuid4(),
        client_user_id=uuid4(),
        service_name="Стрижка",
        start_at=NOON_UTC,
        end_at=NOON_UTC + timedelta(minutes=45),
        cancelled_by=by,
        timezone="Europe/Moscow",
    )


# --- time -------------------------------------------------------------------


def test_a_moment_is_shown_in_the_zone_of_the_salon() -> None:
    assert format_moment(NOON_UTC, "Asia/Yekaterinburg") == "05.09.2026 17:00 (Asia/Yekaterinburg)"


def test_without_a_zone_the_moment_is_shown_in_utc_and_says_so() -> None:
    assert format_moment(NOON_UTC, None) == "05.09.2026 12:00 (UTC)"


def test_an_unknown_zone_falls_back_to_utc() -> None:
    assert format_moment(NOON_UTC, "Mars/Olympus") == "05.09.2026 12:00 (UTC)"


# --- booking messages -------------------------------------------------------


def test_a_new_booking_names_the_service_and_the_local_time() -> None:
    event = BookingCreated(
        booking_id=uuid4(),
        salon_id=uuid4(),
        master_id=uuid4(),
        client_user_id=uuid4(),
        service_id=uuid4(),
        service_name="Стрижка",
        price="3500.00",
        currency="RUB",
        start_at=NOON_UTC,
        end_at=NOON_UTC + timedelta(minutes=45),
        status="confirmed",
        timezone="Europe/Moscow",
    )

    template, fields = created_message(event)

    assert template == "booking_created"
    assert fields == {"service_name": "Стрижка", "start_at": "05.09.2026 15:00 (Europe/Moscow)"}


def test_a_move_names_both_times() -> None:
    event = BookingRescheduled(
        booking_id=uuid4(),
        salon_id=uuid4(),
        master_id=uuid4(),
        client_user_id=uuid4(),
        service_name="Стрижка",
        previous_start_at=NOON_UTC,
        previous_end_at=NOON_UTC + timedelta(minutes=45),
        start_at=NOON_UTC + timedelta(days=1),
        end_at=NOON_UTC + timedelta(days=1, minutes=45),
        timezone="Europe/Moscow",
    )

    template, fields = rescheduled_message(event)

    assert template == "booking_rescheduled"
    assert fields["previous_start_at"] == "05.09.2026 15:00 (Europe/Moscow)"
    assert fields["start_at"] == "06.09.2026 15:00 (Europe/Moscow)"


@pytest.mark.parametrize(
    ("side", "template"),
    [
        (CancelledBy.CLIENT, "booking_cancelled_by_client"),
        (CancelledBy.SALON, "booking_cancelled_by_salon"),
    ],
)
def test_a_cancellation_is_told_by_who_called_it_off(side: CancelledBy, template: str) -> None:
    assert cancelled_message(cancellation(side))[0] == template


def test_the_confirmation_letter_carries_the_link_with_the_token() -> None:
    event = UserEmailConfirmationRequested(
        user_id=uuid4(),
        email="client@example.com",
        token="a+b/c=",  # noqa: S106 - a fixture, not a credential
        expires_at="2026-09-06T14:31:00+00:00",
    )

    template, fields = confirmation_message(event, confirmation_url="http://x/confirm")

    assert template == "email_confirmation"
    assert fields["link"] == "http://x/confirm?token=a%2Bb%2Fc%3D"
    assert fields["expires_at"] == "06.09.2026 14:31 (UTC)"


def test_a_reminder_names_the_visit_at_its_local_time() -> None:
    event = ReminderDue(
        booking_id=uuid4(),
        salon_id=uuid4(),
        master_id=uuid4(),
        client_user_id=uuid4(),
        service_name="Стрижка",
        start_at=NOON_UTC,
        end_at=NOON_UTC + timedelta(minutes=45),
        hours_before=4,
        timezone="Europe/Moscow",
    )

    template, fields = reminder_message(event)

    assert template == "booking_reminder"
    assert fields == {"service_name": "Стрижка", "start_at": "05.09.2026 15:00 (Europe/Moscow)"}
    assert "05.09.2026 15:00 (Europe/Moscow)" in render(template, fields).body


# --- channels ---------------------------------------------------------------


def test_every_reachable_channel_is_used_by_default() -> None:
    assert reachable_channels(recipient(), BOOKINGS) == [Channel.EMAIL, Channel.TELEGRAM]


def test_an_unconfirmed_address_is_not_written_to() -> None:
    assert reachable_channels(recipient(email_confirmed=False), BOOKINGS) == [Channel.TELEGRAM]


def test_without_a_linked_chat_there_is_no_telegram() -> None:
    assert reachable_channels(recipient(telegram_chat_id=None), BOOKINGS) == [Channel.EMAIL]


def test_a_channel_switched_off_for_a_kind_is_not_used_for_it() -> None:
    switched_off = recipient(preferences={"bookings": {"telegram": False}})

    assert reachable_channels(switched_off, BOOKINGS) == [Channel.EMAIL]
    assert reachable_channels(switched_off, REMINDERS) == [Channel.EMAIL, Channel.TELEGRAM]


def test_a_deactivated_account_gets_nothing() -> None:
    assert reachable_channels(recipient(is_active=False), BOOKINGS) == []
