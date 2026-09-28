"""The log provider and the choice of a provider per channel."""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from barber_common.config import Environment
from barber_notification.providers.base import (
    Channel,
    DeliveryOutcome,
    DeliveryResult,
    Message,
)
from barber_notification.providers.log_provider import LogProvider
from barber_notification.providers.registry import ProviderRegistry

MESSAGE = Message(template="booking_created", subject="Вы записаны", body="Текст записи")
SECRET_MESSAGE = Message(
    template="email_confirmation",
    subject="Подтвердите адрес",
    body="http://localhost/confirm?token=kJ9-token",
    is_sensitive=True,
)


class RecordingProvider:
    """A provider that remembers what it was given."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Message]] = []

    @property
    def name(self) -> str:
        return "recording"

    async def send(self, address: str, message: Message) -> DeliveryResult:
        self.sent.append((address, message))
        return DeliveryResult.sent()


async def test_the_log_provider_writes_the_message_and_reports_it_sent() -> None:
    provider = LogProvider(channel=Channel.EMAIL, environment=Environment.LOCAL)

    with capture_logs() as records:
        result = await provider.send("client@example.com", MESSAGE)

    assert result.outcome is DeliveryOutcome.SENT
    [record] = [entry for entry in records if entry["event"] == "message"]
    assert record["channel"] == "email"
    assert record["template"] == "booking_created"
    assert record["address"] == "client@example.com"
    assert record["body"] == "Текст записи"


@pytest.mark.parametrize("environment", [Environment.DEV, Environment.PROD])
async def test_outside_local_and_test_neither_text_nor_address_is_written(
    environment: Environment,
) -> None:
    provider = LogProvider(channel=Channel.EMAIL, environment=environment)

    with capture_logs() as records:
        result = await provider.send("client@example.com", SECRET_MESSAGE)

    assert result.outcome is DeliveryOutcome.SENT
    written = repr(records)
    assert "kJ9-token" not in written
    assert "client@example.com" not in written
    assert "email_confirmation" in written


def test_without_a_bot_telegram_is_delivered_to_the_log() -> None:
    registry = ProviderRegistry.build(environment=Environment.TEST)

    assert registry.for_channel(Channel.TELEGRAM).name == "log"
    assert registry.for_channel(Channel.EMAIL).name == "log"


def test_a_configured_bot_delivers_telegram() -> None:
    bot = RecordingProvider()

    registry = ProviderRegistry.build(environment=Environment.TEST, telegram=bot)

    assert registry.for_channel(Channel.TELEGRAM) is bot
    assert registry.for_channel(Channel.EMAIL).name == "log"


def test_a_registry_without_a_provider_for_a_channel_is_refused() -> None:
    with pytest.raises(ValueError, match="telegram"):
        ProviderRegistry({Channel.EMAIL: RecordingProvider()})


def test_a_temporary_failure_carries_its_retry_hint() -> None:
    result = DeliveryResult.temporary("rate limited", retry_after_seconds=3.0)

    assert (result.outcome, result.retry_after_seconds) == (
        DeliveryOutcome.TEMPORARY_FAILURE,
        3.0,
    )
