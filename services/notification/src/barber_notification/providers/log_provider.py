"""A provider that writes messages to a log instead of sending them.

The provider of the ``email`` channel until there is a mail provider, and of
``telegram`` when no bot token is configured. The messages go to a logger of
their own, ``barber_notification.messages``, so every query that reads them is
separate from the service log.

The text and the address are written only in the ``local`` and ``test``
environments. Anywhere else the record says that a message went out, and not
what it said or to whom: an address is personal data, and a confirmation letter
carries a credential.
"""

from __future__ import annotations

from barber_common.config import Environment
from barber_common.logging import get_logger
from barber_notification.providers.base import Channel, DeliveryResult, Message

__all__ = ["LogProvider"]

# Not get_logger(__name__): the name is what separates outgoing messages from
# the service log.
_message_logger = get_logger("barber_notification.messages")

_ENVIRONMENTS_THAT_SHOW_TEXT = (Environment.LOCAL, Environment.TEST)


class LogProvider:
    """Writes each message where a developer can read it."""

    def __init__(self, *, channel: Channel, environment: Environment) -> None:
        self._channel = channel
        self._environment = environment

    @property
    def name(self) -> str:
        return "log"

    @property
    def shows_text(self) -> bool:
        """Whether the text and the address may be written in this environment."""
        return self._environment in _ENVIRONMENTS_THAT_SHOW_TEXT

    async def send(self, address: str, message: Message) -> DeliveryResult:
        """Never fails: a log that cannot be written is not a delivery problem."""
        if self.shows_text:
            _message_logger.info(
                "message",
                channel=self._channel.value,
                template=message.template,
                address=address,
                subject=message.subject,
                body=message.body,
            )
        else:
            _message_logger.info(
                "message",
                channel=self._channel.value,
                template=message.template,
            )
        return DeliveryResult.sent()
