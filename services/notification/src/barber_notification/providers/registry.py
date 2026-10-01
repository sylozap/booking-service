"""Which provider delivers each channel, chosen once from the configuration."""

from __future__ import annotations

from collections.abc import Mapping

from barber_common.config import Environment
from barber_notification.providers.base import Channel, Provider
from barber_notification.providers.log_provider import LogProvider

__all__ = ["ProviderRegistry"]


class ProviderRegistry:
    """A provider per channel.

    Built at startup and never changed after: the choice is configuration, not
    a decision made per message.
    """

    def __init__(self, providers: Mapping[Channel, Provider]) -> None:
        missing = set(Channel) - set(providers)
        if missing:
            names = ", ".join(sorted(channel.value for channel in missing))
            raise ValueError(f"no provider for channel(s): {names}")
        self._providers = dict(providers)

    @classmethod
    def build(
        cls,
        *,
        environment: Environment,
        telegram: Provider | None = None,
    ) -> ProviderRegistry:
        """The registry of the running service.

        ``email`` always goes to the log: there is no mail provider. ``telegram``
        goes to the bot when one is configured and to the log otherwise, so a
        missing token costs the channel, not the start of the service.
        """
        return cls(
            {
                Channel.EMAIL: LogProvider(channel=Channel.EMAIL, environment=environment),
                Channel.TELEGRAM: telegram
                or LogProvider(channel=Channel.TELEGRAM, environment=environment),
            }
        )

    def for_channel(self, channel: Channel) -> Provider:
        return self._providers[channel]
