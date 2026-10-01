"""The rules of a user's notification switches, without a database."""

from __future__ import annotations

from barber_notification.preferences import NotificationKind, Preferences
from barber_notification.providers.base import Channel

BOOKINGS = NotificationKind.BOOKINGS
REMINDERS = NotificationKind.REMINDERS


def test_everything_is_on_until_switched_off() -> None:
    preferences = Preferences.from_stored({})

    assert all(
        preferences.allows(kind, channel) for kind in NotificationKind for channel in Channel
    )


def test_a_stored_false_switches_off_exactly_that_kind_on_that_channel() -> None:
    preferences = Preferences.from_stored({"reminders": {"telegram": False}})

    assert preferences.allows(REMINDERS, Channel.TELEGRAM) is False
    assert preferences.allows(REMINDERS, Channel.EMAIL) is True
    assert preferences.allows(BOOKINGS, Channel.TELEGRAM) is True


def test_a_value_that_cannot_be_read_leaves_the_switch_on() -> None:
    stored: dict[str, object] = {
        "bookings": "all of them",
        "reminders": {"email": "no", "telegram": 0},
    }

    preferences = Preferences.from_stored(stored)

    assert preferences == Preferences()


def test_changes_flip_only_the_switches_they_name() -> None:
    current = Preferences.from_stored({"bookings": {"email": False}})

    changed = current.with_changes({REMINDERS: {Channel.TELEGRAM: False}})

    assert changed.switched_off == {(BOOKINGS, Channel.EMAIL), (REMINDERS, Channel.TELEGRAM)}


def test_a_switch_turned_on_again_is_on() -> None:
    current = Preferences.from_stored({"bookings": {"email": False}})

    changed = current.with_changes({BOOKINGS: {Channel.EMAIL: True}})

    assert changed == Preferences()


def test_every_switch_may_be_turned_off() -> None:
    everything = {kind: dict.fromkeys(Channel, False) for kind in NotificationKind}

    changed = Preferences().with_changes(everything)

    assert not any(
        changed.allows(kind, channel) for kind in NotificationKind for channel in Channel
    )


def test_the_whole_matrix_is_stored_and_read_back_the_same() -> None:
    preferences = Preferences().with_changes({BOOKINGS: {Channel.TELEGRAM: False}})

    stored = preferences.to_stored()

    assert stored == {
        "bookings": {"email": True, "telegram": False},
        "reminders": {"email": True, "telegram": True},
    }
    assert Preferences.from_stored(stored) == preferences
