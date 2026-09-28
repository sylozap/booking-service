"""The Telegram adapter against a mocked Bot API.

Nothing here reaches Telegram: the transport answers the way the Bot API
documents it does, including the ways it refuses.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

import httpx
import pytest
from opentelemetry.instrumentation.utils import is_http_instrumentation_enabled
from pydantic import SecretStr

from barber_notification.providers.base import DeliveryOutcome, Message
from barber_notification.providers.telegram import (
    TelegramBotApi,
    TelegramProvider,
    TelegramUnavailable,
)
from barber_notification.settings import NotificationSettings

TOKEN = "123456:secret-bot-token"  # noqa: S105 - a fixture, not a credential
MESSAGE = Message(template="booking_created", subject="Вы записаны", body="Текст")

Handler = Callable[[httpx.Request], httpx.Response]


def api_answering(handler: Handler) -> TelegramBotApi:
    return TelegramBotApi(
        token=SecretStr(TOKEN),
        base_url="https://telegram.test",
        transport=httpx.MockTransport(handler),
    )


def refusal(status: int, description: str, **parameters: object) -> httpx.Response:
    body: dict[str, object] = {"ok": False, "error_code": status, "description": description}
    if parameters:
        body["parameters"] = parameters
    return httpx.Response(status, json=body)


async def test_a_message_is_sent_to_the_chat() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    result = await TelegramProvider(api_answering(handler)).send("42", MESSAGE)

    assert result.outcome is DeliveryOutcome.SENT
    [request] = requests
    assert request.url.path == f"/bot{TOKEN}/sendMessage"
    assert json.loads(request.content) == {"chat_id": 42, "text": "Вы записаны\n\nТекст"}


async def test_a_rate_limit_is_temporary_and_says_when_to_come_back() -> None:
    api = api_answering(lambda _: refusal(429, "Too Many Requests: retry after 7", retry_after=7))

    result = await TelegramProvider(api).send("42", MESSAGE)

    assert result.outcome is DeliveryOutcome.TEMPORARY_FAILURE
    assert result.retry_after_seconds == 7.0


async def test_a_server_error_is_temporary() -> None:
    api = api_answering(lambda _: refusal(502, "Bad Gateway"))

    result = await TelegramProvider(api).send("42", MESSAGE)

    assert result.outcome is DeliveryOutcome.TEMPORARY_FAILURE


async def test_a_timeout_is_temporary() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    result = await TelegramProvider(api_answering(handler)).send("42", MESSAGE)

    assert result.outcome is DeliveryOutcome.TEMPORARY_FAILURE


async def test_a_blocked_bot_is_permanent_and_the_chat_is_gone() -> None:
    api = api_answering(lambda _: refusal(403, "Forbidden: bot was blocked by the user"))

    result = await TelegramProvider(api).send("42", MESSAGE)

    assert result.outcome is DeliveryOutcome.PERMANENT_FAILURE
    assert result.address_gone is True


async def test_a_missing_chat_is_permanent_and_the_chat_is_gone() -> None:
    api = api_answering(lambda _: refusal(400, "Bad Request: chat not found"))

    result = await TelegramProvider(api).send("42", MESSAGE)

    assert (result.outcome, result.address_gone) == (DeliveryOutcome.PERMANENT_FAILURE, True)


async def test_another_bad_request_is_permanent_but_keeps_the_chat() -> None:
    api = api_answering(lambda _: refusal(400, "Bad Request: message text is empty"))

    result = await TelegramProvider(api).send("42", MESSAGE)

    assert (result.outcome, result.address_gone) == (DeliveryOutcome.PERMANENT_FAILURE, False)


async def test_an_address_that_is_not_a_chat_id_is_permanent() -> None:
    api = api_answering(lambda _: httpx.Response(200, json={"ok": True, "result": {}}))

    result = await TelegramProvider(api).send("client@example.com", MESSAGE)

    assert result.outcome is DeliveryOutcome.PERMANENT_FAILURE


async def test_the_token_never_appears_in_a_failure() -> None:
    api = api_answering(lambda _: refusal(500, "Internal Server Error"))

    result = await TelegramProvider(api).send("42", MESSAGE)

    assert result.detail is not None
    assert TOKEN not in result.detail


async def test_no_span_is_made_of_a_call_whose_url_carries_the_token() -> None:
    # The chassis instruments httpx for the whole process; this is the switch
    # its wrapper reads before it records a URL.
    seen: list[bool] = []

    def handler(_: httpx.Request) -> httpx.Response:
        seen.append(is_http_instrumentation_enabled())
        return httpx.Response(200, json={"ok": True, "result": []})

    api = api_answering(handler)
    await api.send_message(chat_id=42, text="hi")
    await api.get_updates(offset=None, timeout_seconds=0)

    assert seen == [False, False]
    assert is_http_instrumentation_enabled() is True


def test_the_request_line_httpx_logs_has_the_token_redacted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    api_answering(lambda _: httpx.Response(200))

    with caplog.at_level(logging.INFO, logger="httpx"):
        logging.getLogger("httpx").info(
            "HTTP Request: %s %s", "POST", f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        )

    assert TOKEN not in caplog.text
    assert "<redacted>" in caplog.text


async def test_updates_are_read_from_the_offset_given() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {
                        "update_id": 10,
                        "message": {
                            "message_id": 1,
                            "chat": {"id": 42, "type": "private"},
                            "text": "/start abc",
                        },
                    },
                    {"update_id": 11, "edited_message": {"message_id": 2}},
                ],
            },
        )

    updates = await api_answering(handler).get_updates(offset=10, timeout_seconds=0)

    assert [update.update_id for update in updates] == [10, 11]
    assert updates[0].message is not None
    assert updates[0].message.text == "/start abc"
    assert updates[1].message is None
    assert seen[0].url.params["offset"] == "10"


async def test_a_failed_poll_raises_without_the_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(TelegramUnavailable) as failure:
        await api_answering(handler).get_updates(offset=None, timeout_seconds=0)

    assert TOKEN not in str(failure.value)


async def test_a_conflicting_poller_is_reported_as_unavailable() -> None:
    api = api_answering(lambda _: refusal(409, "Conflict: terminated by other getUpdates request"))

    with pytest.raises(TelegramUnavailable):
        await api.get_updates(offset=None, timeout_seconds=0)


def telegram_settings(**telegram: object) -> NotificationSettings:
    return NotificationSettings(
        environment="test",
        database_dsn="postgresql+asyncpg://n:n@localhost:5432/n",
        redis_dsn="redis://localhost:6379/0",
        kafka_bootstrap_servers="localhost:9092",
        **telegram,  # type: ignore[arg-type]  # settings fields are typed per key
    )


def test_without_a_token_telegram_is_not_enabled() -> None:
    settings = telegram_settings(telegram_bot_username="barber_bot")

    assert settings.telegram_enabled is False


def test_an_empty_token_counts_as_no_token() -> None:
    settings = telegram_settings(telegram_bot_token="", telegram_bot_username="barber_bot")

    assert settings.telegram_enabled is False


def test_a_token_and_a_bot_name_enable_telegram() -> None:
    settings = telegram_settings(telegram_bot_token=TOKEN, telegram_bot_username="barber_bot")

    assert settings.telegram_enabled is True
