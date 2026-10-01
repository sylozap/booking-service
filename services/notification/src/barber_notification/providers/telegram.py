"""Telegram: the Bot API client and the provider of the ``telegram`` channel.

**The token is part of every URL** (``/bot<token>/sendMessage``), so two things
that would normally write URLs are kept away from these calls: the
OpenTelemetry instrumentation of httpx, which records the URL in the span and is
suppressed around every call, and the ``HTTP Request: POST <url>`` line httpx
logs at INFO, which is redacted.
Error details kept from a response are Telegram's own description, never the
request.

Failures are sorted in two:

* temporary -- a timeout, a refused connection, a 5xx, ``429`` with its
  ``retry_after``: another attempt later may succeed;
* permanent -- the user blocked the bot, the chat does not exist, the request
  was refused: another attempt fails the same way. A blocked bot and a missing
  chat also mean the address is dead.
"""

from __future__ import annotations

import logging

import httpx
from opentelemetry.instrumentation.utils import suppress_http_instrumentation
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from barber_notification.providers.base import DeliveryResult, Message

__all__ = [
    "TelegramBotApi",
    "TelegramChat",
    "TelegramMessage",
    "TelegramProvider",
    "TelegramUnavailable",
    "TelegramUpdate",
]

_TOO_MANY_REQUESTS = 429
_FORBIDDEN = 403
_BAD_REQUEST = 400
_SERVER_ERROR = 500
# The words of a 400 that mean the chat itself is gone, not the request wrong.
_CHAT_GONE_MARKERS = ("chat not found", "user is deactivated")
# Telegram refuses longer texts; cutting is better than losing the message.
_MAX_TEXT_LENGTH = 4096


class TelegramUnavailable(Exception):
    """The Bot API could not be reached or did not answer as documented."""


class TelegramChat(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: int
    type: str


class TelegramMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    message_id: int
    chat: TelegramChat
    text: str | None = None


class TelegramUpdate(BaseModel):
    """One update of ``getUpdates``. Only messages are asked for."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    update_id: int
    message: TelegramMessage | None = None


class _ApiAnswer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    ok: bool
    result: object = None
    error_code: int | None = None
    description: str | None = None
    parameters: dict[str, object] | None = None


class _RedactToken(logging.Filter):
    """Replaces the bot token in the request lines httpx logs."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if self._token in message:
            record.msg = message.replace(self._token, "<redacted>")
            record.args = ()
        return True


def _redact_in_http_logs(token: str) -> None:
    """Install the redaction once per token on the logger httpx writes to."""
    http_logger = logging.getLogger("httpx")
    for existing in http_logger.filters:
        if isinstance(existing, _RedactToken) and existing._token == token:
            return
    http_logger.addFilter(_RedactToken(token))


class TelegramBotApi:
    """The two Bot API methods this service calls, with a timeout on each."""

    def __init__(
        self,
        *,
        token: SecretStr,
        base_url: str = "https://api.telegram.org",
        timeout_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        secret = token.get_secret_value()
        self._timeout_seconds = timeout_seconds
        self._client = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/bot{secret}",
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        )
        _redact_in_http_logs(secret)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def send_message(self, *, chat_id: int, text: str) -> DeliveryResult:
        """Send one text. Every failure is a result, never an exception."""
        try:
            # The span of the call would carry the URL, and the URL the token.
            # The chassis instruments httpx at the class level, so it has to be
            # switched off around the call rather than on this client.
            with suppress_http_instrumentation():
                response = await self._client.post(
                    "/sendMessage",
                    json={"chat_id": chat_id, "text": text[:_MAX_TEXT_LENGTH]},
                )
        except httpx.TimeoutException:
            return DeliveryResult.temporary("telegram: timeout")
        except httpx.TransportError as error:
            return DeliveryResult.temporary(f"telegram: {type(error).__name__}")
        return _classify(response)

    async def get_updates(
        self, *, offset: int | None, timeout_seconds: int
    ) -> list[TelegramUpdate]:
        """Wait up to ``timeout_seconds`` for messages to the bot.

        Asking with ``offset`` confirms every update below it: Telegram stops
        returning them.
        """
        params: dict[str, str | int] = {
            "timeout": timeout_seconds,
            "allowed_updates": '["message"]',
        }
        if offset is not None:
            params["offset"] = offset
        try:
            with suppress_http_instrumentation():
                response = await self._client.get(
                    "/getUpdates",
                    params=params,
                    # The server holds the request for the poll timeout; the
                    # read timeout has to outlast it.
                    timeout=httpx.Timeout(self._timeout_seconds + timeout_seconds),
                )
        except httpx.HTTPError as error:
            raise TelegramUnavailable(f"getUpdates failed: {type(error).__name__}") from None

        answer = _answer_of(response)
        if answer is None or not answer.ok or not isinstance(answer.result, list):
            raise TelegramUnavailable(f"getUpdates answered {response.status_code}")
        try:
            return [TelegramUpdate.model_validate(update) for update in answer.result]
        except ValidationError as error:
            raise TelegramUnavailable("getUpdates answered an unexpected shape") from error


class TelegramProvider:
    """Delivers the ``telegram`` channel. The address is the chat id."""

    def __init__(self, api: TelegramBotApi) -> None:
        self._api = api

    @property
    def name(self) -> str:
        return "telegram"

    async def send(self, address: str, message: Message) -> DeliveryResult:
        try:
            chat_id = int(address)
        except ValueError:
            return DeliveryResult.permanent("telegram: the address is not a chat id")
        return await self._api.send_message(
            chat_id=chat_id, text=f"{message.subject}\n\n{message.body}"
        )


def _answer_of(response: httpx.Response) -> _ApiAnswer | None:
    try:
        return _ApiAnswer.model_validate_json(response.content)
    except ValidationError:
        return None


def _classify(response: httpx.Response) -> DeliveryResult:
    """Sort an answer of sendMessage into sent, temporary and permanent."""
    answer = _answer_of(response)
    if answer is not None and answer.ok:
        return DeliveryResult.sent()

    status = response.status_code
    description = (answer.description if answer else None) or f"HTTP {status}"
    detail = f"telegram {status}: {description}"

    if status == _TOO_MANY_REQUESTS:
        return DeliveryResult.temporary(detail, retry_after_seconds=_retry_after(answer))
    if status >= _SERVER_ERROR or answer is None:
        return DeliveryResult.temporary(detail)
    if status == _FORBIDDEN:
        # Blocked by the user, or the user is gone: nothing reaches this chat.
        return DeliveryResult.permanent(detail, address_gone=True)
    if status == _BAD_REQUEST and any(
        marker in description.lower() for marker in _CHAT_GONE_MARKERS
    ):
        return DeliveryResult.permanent(detail, address_gone=True)
    return DeliveryResult.permanent(detail)


def _retry_after(answer: _ApiAnswer | None) -> float | None:
    if answer is None or answer.parameters is None:
        return None
    value = answer.parameters.get("retry_after")
    return float(value) if isinstance(value, int | float) else None
