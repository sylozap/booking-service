"""What every provider is: a message in, a result out.

A provider knows nothing about Kafka, the database or the journal. It gets a
finished message and an address, delivers or fails, and says which kind of
failure it was -- the delivery worker decides what to do about it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

__all__ = [
    "Channel",
    "DeliveryOutcome",
    "DeliveryResult",
    "Message",
    "Provider",
]


class Channel(StrEnum):
    """Where a message goes. Also the column ``notifications.channel``.

    A channel is not a provider: ``email`` is delivered by the log provider
    until there is a mail provider, and ``telegram`` falls back to the log when
    no bot token is configured.
    """

    EMAIL = "email"
    TELEGRAM = "telegram"


@dataclass(frozen=True, slots=True)
class Message:
    """A rendered message, ready to go.

    ``is_sensitive`` marks a text that carries a credential -- the link of an
    email confirmation. Such a text is never written to a log outside the local
    and test environments.
    """

    template: str
    subject: str
    body: str
    is_sensitive: bool = False


class DeliveryOutcome(StrEnum):
    """What happened to one attempt."""

    SENT = "sent"
    # Worth another attempt: a timeout, a 5xx, a rate limit.
    TEMPORARY_FAILURE = "temporary_failure"
    # Another attempt fails the same way: the user blocked the bot, the chat
    # does not exist.
    PERMANENT_FAILURE = "permanent_failure"


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """The outcome of one attempt and what the worker needs to act on it.

    ``detail`` is safe to store and to log: no address, no token, no text.
    ``address_gone`` says the address itself is dead -- the chat was blocked --
    so the recipient should stop being offered this channel.
    """

    outcome: DeliveryOutcome
    detail: str | None = None
    retry_after_seconds: float | None = None
    address_gone: bool = False

    @classmethod
    def sent(cls) -> DeliveryResult:
        return cls(DeliveryOutcome.SENT)

    @classmethod
    def temporary(cls, detail: str, *, retry_after_seconds: float | None = None) -> DeliveryResult:
        return cls(
            DeliveryOutcome.TEMPORARY_FAILURE,
            detail=detail,
            retry_after_seconds=retry_after_seconds,
        )

    @classmethod
    def permanent(cls, detail: str, *, address_gone: bool = False) -> DeliveryResult:
        return cls(DeliveryOutcome.PERMANENT_FAILURE, detail=detail, address_gone=address_gone)


class Provider(Protocol):
    """Delivers a message to an address on its channel."""

    @property
    def name(self) -> str:
        """Short name for logs and metrics: ``log``, ``telegram``."""
        ...

    async def send(self, address: str, message: Message) -> DeliveryResult:
        """Deliver once. Never raises for a failure of the delivery itself."""
        ...
