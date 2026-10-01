"""The rules that keep a recipient right when events arrive late or out of order."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from barber_notification.repositories.recipients import RecipientRecord
from barber_notification.services.recipients import apply_confirmation, apply_contacts

MORNING = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


def recipient(**overrides: object) -> RecipientRecord:
    fields: dict[str, object] = {
        "user_id": uuid4(),
        "email": None,
        "phone": None,
        "email_confirmed": False,
        "telegram_chat_id": None,
        "is_active": True,
        "preferences": {},
        "contacts_updated_at": None,
    }
    fields.update(overrides)
    return RecipientRecord(**fields)  # type: ignore[arg-type]  # fields are typed per key


def test_a_first_snapshot_fills_the_contacts() -> None:
    empty = recipient()

    filled = apply_contacts(
        empty, email="a@example.com", phone="+7999", email_confirmed=False, taken_at=MORNING
    )

    assert (filled.email, filled.phone, filled.contacts_updated_at) == (
        "a@example.com",
        "+7999",
        MORNING,
    )


def test_an_older_snapshot_leaves_a_newer_one_in_place() -> None:
    current = recipient(email="new@example.com", phone="+7111", contacts_updated_at=MORNING)

    after = apply_contacts(
        current,
        email="old@example.com",
        phone="+7000",
        email_confirmed=False,
        taken_at=MORNING - timedelta(minutes=5),
    )

    assert after == current


def test_a_confirmation_of_the_same_address_survives_an_older_style_snapshot() -> None:
    confirmed = recipient(email="a@example.com", email_confirmed=True)

    after = apply_contacts(
        confirmed, email="a@example.com", phone="+7999", email_confirmed=False, taken_at=MORNING
    )

    assert after.email_confirmed is True


def test_a_new_address_starts_unconfirmed() -> None:
    confirmed = recipient(email="a@example.com", email_confirmed=True, contacts_updated_at=MORNING)

    after = apply_contacts(
        confirmed,
        email="b@example.com",
        phone="+7999",
        email_confirmed=False,
        taken_at=MORNING + timedelta(hours=1),
    )

    assert (after.email, after.email_confirmed) == ("b@example.com", False)


def test_a_confirmation_that_overtook_the_registration_keeps_the_address() -> None:
    empty = recipient()

    after = apply_confirmation(empty, email="a@example.com")

    assert (after.email, after.email_confirmed) == ("a@example.com", True)


def test_a_confirmation_of_an_address_changed_since_confirms_nothing() -> None:
    current = recipient(email="b@example.com", email_confirmed=False)

    after = apply_confirmation(current, email="a@example.com")

    assert after == current


def test_a_confirmation_marks_the_current_address() -> None:
    current = recipient(email="a@example.com")

    after = apply_confirmation(current, email="a@example.com")

    assert after == replace(current, email_confirmed=True)
