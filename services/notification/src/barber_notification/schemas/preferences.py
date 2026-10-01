"""Requests and responses of the notification switches."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from barber_notification.preferences import (
    NotificationKind,
    PreferenceChanges,
    Preferences,
)
from barber_notification.providers.base import Channel

__all__ = ["PreferencesPatch", "PreferencesResponse"]


class ChannelSwitches(BaseModel):
    """Whether a kind of notification goes to each channel."""

    model_config = ConfigDict(frozen=True)

    email: bool
    telegram: bool


class PreferencesResponse(BaseModel):
    """Every switch of the caller. A channel that is on still needs an address:
    a confirmed email, a linked Telegram chat."""

    model_config = ConfigDict(frozen=True)

    bookings: ChannelSwitches = Field(description="A booking was made, moved or cancelled")
    reminders: ChannelSwitches = Field(description="A visit is coming up")

    @classmethod
    def of(cls, preferences: Preferences) -> PreferencesResponse:
        def switches(kind: NotificationKind) -> ChannelSwitches:
            return ChannelSwitches(
                email=preferences.allows(kind, Channel.EMAIL),
                telegram=preferences.allows(kind, Channel.TELEGRAM),
            )

        return cls(
            bookings=switches(NotificationKind.BOOKINGS),
            reminders=switches(NotificationKind.REMINDERS),
        )


class ChannelSwitchesPatch(BaseModel):
    """Switches to flip. A channel left out, or ``null``, stays as it is."""

    model_config = ConfigDict(extra="forbid")

    email: StrictBool | None = None
    telegram: StrictBool | None = None


class PreferencesPatch(BaseModel):
    """Switches to flip, by kind. Anything left out stays as it is."""

    model_config = ConfigDict(extra="forbid")

    bookings: ChannelSwitchesPatch | None = None
    reminders: ChannelSwitchesPatch | None = None

    def changes(self) -> PreferenceChanges:
        """The switches this request names, and nothing else."""
        changes: dict[NotificationKind, dict[Channel, bool]] = {}
        for kind, patch in (
            (NotificationKind.BOOKINGS, self.bookings),
            (NotificationKind.REMINDERS, self.reminders),
        ):
            if patch is None:
                continue
            named = {
                channel: enabled
                for channel, enabled in (
                    (Channel.EMAIL, patch.email),
                    (Channel.TELEGRAM, patch.telegram),
                )
                if enabled is not None
            }
            if named:
                changes[kind] = named
        return changes
