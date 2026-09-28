"""Reading what users send to the bot, by long polling.

Long polling rather than a webhook: a webhook needs a public HTTPS address,
which the local cluster does not have. The cost is that Telegram allows one
poller per bot -- a second ``getUpdates`` is answered ``409`` -- so only one
replica polls. Which one is decided by a PostgreSQL advisory lock, held on a
connection of its own for as long as the replica polls: when the pod dies, the
connection closes, the lock is released, and another replica takes over.

The offset of the next update lives in memory. Asking for it confirms the ones
before, so a replica that dies between handling an update and the next poll
sees that update again; handling it twice is harmless, the code is spent.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from barber_common.db.session import unit_of_work
from barber_common.logging import get_logger
from barber_notification.providers.telegram import (
    TelegramBotApi,
    TelegramUnavailable,
    TelegramUpdate,
)
from barber_notification.services.telegram_link import LinkTelegramChat

__all__ = ["TELEGRAM_POLLER_LOCK", "TelegramUpdatesWorker"]

_logger = get_logger(__name__)

# The key of the advisory lock that elects the poller. Any constant will do as
# long as nothing else in this database uses it.
TELEGRAM_POLLER_LOCK = 7_204_101

_START_COMMAND = "/start"

LINKED_REPLY = "Готово: уведомления о записях будут приходить сюда."
INVALID_CODE_REPLY = (
    "Код не подошёл: он неверный, уже использован или истёк. "
    "Получите новую ссылку в личном кабинете."
)
HELP_REPLY = "Чтобы получать уведомления здесь, откройте ссылку из личного кабинета."


class TelegramUpdatesWorker:
    """Polls the bot while this replica holds the lock, and links chats."""

    def __init__(
        self,
        *,
        api: TelegramBotApi,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
        poll_timeout_seconds: int = 25,
        idle_interval_seconds: float = 5.0,
    ) -> None:
        self._api = api
        self._engine = engine
        self._session_factory = session_factory
        self._poll_timeout_seconds = poll_timeout_seconds
        self._idle_interval_seconds = idle_interval_seconds
        self._offset: int | None = None

    async def run_once(self) -> int:
        """One poll: wait for updates, handle them, remember where to go on."""
        updates = await self._api.get_updates(
            offset=self._offset, timeout_seconds=self._poll_timeout_seconds
        )
        for update in updates:
            await self.handle(update)
            self._offset = update.update_id + 1
        return len(updates)

    async def handle(self, update: TelegramUpdate) -> None:
        """Link the chat for ``/start <code>``; answer anything else with a hint."""
        message = update.message
        # Groups and channels are not anybody's personal address.
        if message is None or message.chat.type != "private":
            return

        command, _, argument = (message.text or "").strip().partition(" ")
        if command != _START_COMMAND or not argument.strip():
            await self._reply(message.chat.id, HELP_REPLY)
            return

        async with unit_of_work(self._session_factory) as session:
            user_id = await LinkTelegramChat(session).execute(
                code=argument.strip(), chat_id=message.chat.id
            )
        await self._reply(message.chat.id, LINKED_REPLY if user_id else INVALID_CODE_REPLY)

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Poll while elected; otherwise check the election now and then."""
        _logger.info("telegram updates worker started")
        while not stop.is_set():
            try:
                async with _poller_lock(self._engine) as elected:
                    if elected:
                        _logger.info("this replica polls telegram")
                        await self._poll_until(stop)
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("telegram updates worker failed")
            await _wait(stop, self._idle_interval_seconds)
        _logger.info("telegram updates worker stopped")

    @asynccontextmanager
    async def run_in_background(self) -> AsyncIterator[None]:
        """Run the worker for as long as the block lasts.

        Cancelled rather than only signalled on the way out: a long poll can
        hold for the whole poll timeout, and shutdown should not wait for it.
        """
        stop = asyncio.Event()
        task = asyncio.create_task(self.run_forever(stop), name="telegram-updates")
        try:
            yield
        finally:
            stop.set()
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _poll_until(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.run_once()
            except TelegramUnavailable as error:
                # Expected now and then: the network, Telegram, a 409 while an
                # old replica still finishes its poll.
                _logger.warning("telegram poll failed", error=str(error))
                await _wait(stop, self._idle_interval_seconds)

    async def _reply(self, chat_id: int, text_: str) -> None:
        """Best effort: a lost reply does not undo the link."""
        result = await self._api.send_message(chat_id=chat_id, text=text_)
        if result.detail is not None:
            _logger.warning("telegram reply not delivered", detail=result.detail)


@asynccontextmanager
async def _poller_lock(engine: AsyncEngine) -> AsyncIterator[bool]:
    """Try to become the poller; yields whether this replica is the one.

    The lock is session level and the connection autocommits: an idle
    transaction held open for hours would be a problem of its own.
    """
    async with engine.connect() as connection:
        await connection.execution_options(isolation_level="AUTOCOMMIT")
        elected = bool(
            await connection.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": TELEGRAM_POLLER_LOCK}
            )
        )
        try:
            yield elected
        finally:
            if elected:
                await connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": TELEGRAM_POLLER_LOCK}
                )


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    try:
        async with asyncio.timeout(seconds):
            await stop.wait()
    except TimeoutError:
        return
