"""Templates: what they render, and how they fail."""

from __future__ import annotations

import pytest

from barber_notification.rendering import TemplateFieldMissing, UnknownTemplate, render

BOOKING_FIELDS: dict[str, object] = {
    "service_name": "Мужская стрижка",
    "start_at": "05.09.2026 15:00 (Europe/Moscow)",
}


def test_a_template_renders_its_subject_and_body() -> None:
    message = render("booking_created", BOOKING_FIELDS)

    assert message.subject == "Вы записаны: Мужская стрижка"
    assert "«Мужская стрижка» — 05.09.2026 15:00 (Europe/Moscow)" in message.body
    assert message.template == "booking_created"


def test_a_missing_field_names_the_field_and_the_template() -> None:
    with pytest.raises(TemplateFieldMissing) as failure:
        render("booking_created", {"service_name": "Мужская стрижка"})

    assert (failure.value.template, failure.value.field) == ("booking_created", "start_at")
    assert "start_at" in str(failure.value)


def test_a_missing_field_never_leaves_a_placeholder_in_the_text() -> None:
    # safe_substitute would hand the client "$start_at"; the error is the point.
    with pytest.raises(TemplateFieldMissing):
        render("booking_rescheduled", BOOKING_FIELDS)


def test_extra_fields_are_ignored() -> None:
    message = render("booking_created", {**BOOKING_FIELDS, "price": "3500.00"})

    assert "3500" not in message.body


def test_an_unknown_template_is_an_error() -> None:
    with pytest.raises(UnknownTemplate):
        render("booking_teleported", BOOKING_FIELDS)


def test_a_template_name_cannot_reach_outside_the_directory() -> None:
    with pytest.raises(UnknownTemplate):
        render("../settings", BOOKING_FIELDS)


def test_the_confirmation_letter_is_marked_sensitive() -> None:
    message = render(
        "email_confirmation",
        {"link": "http://localhost/confirm?token=abc", "expires_at": "02.09.2026 14:31 UTC"},
    )

    assert message.is_sensitive is True
    assert "http://localhost/confirm?token=abc" in message.body


def test_a_booking_message_is_not_sensitive() -> None:
    assert render("booking_created", BOOKING_FIELDS).is_sensitive is False


@pytest.mark.parametrize(
    ("template", "fields"),
    [
        ("booking_created", BOOKING_FIELDS),
        ("booking_rescheduled", {**BOOKING_FIELDS, "previous_start_at": "04.09.2026 12:00"}),
        ("booking_cancelled_by_client", BOOKING_FIELDS),
        ("booking_cancelled_by_salon", BOOKING_FIELDS),
        ("email_confirmation", {"link": "http://x", "expires_at": "tomorrow"}),
    ],
)
def test_every_shipped_template_renders(template: str, fields: dict[str, object]) -> None:
    message = render(template, fields)

    assert message.subject
    assert "$" not in message.subject + message.body
