"""The bot's side of linking: /start with a code, against a real database.

The Bot API is a mock transport; the codes, the recipients and the advisory
lock are real.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from barber_notification.models.recipient import Recipient
from barber_notification.providers.telegram import (
    TelegramBotApi,
    TelegramChat,
    TelegramMessage,
    TelegramUpdate,
)
from barber_notification.repositories.recipients import RecipientRepository
from barber_notification.services.telegram_link import IssueTelegramLinkCode
from barber_notification.workers.telegram_updates import (
    HELP_REPLY,
    INVALID_CODE_REPLY,
    LINKED_REPLY,
    TelegramUpdatesWorker,
    _poller_lock,
)

pytestmark = pytest.mark.integration

RecipientFactory = Callable[..., Awaitable[Recipient]]

CHAT_ID = 777


class FakeBot:
    """The Bot API: remembers what the service said, hands out queued updates."""

    def __init__(self) -> None:
        self.replies: list[tuple[int, str]] = []
        self.updates: list[dict[str, object]] = []
        self.offsets: list[str | None] = []

    def api(self) -> TelegramBotApi:
        return TelegramBotApi(
            token=SecretStr("123:token"),
            base_url="https://telegram.test",
            transport=httpx.MockTransport(self._handle),
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sendMessage"):
            body = json.loads(request.content)
            self.replies.append((body["chat_id"], body["text"]))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        self.offsets.append(request.url.params.get("offset"))
        updates, self.updates = self.updates, []
        return httpx.Response(200, json={"ok": True, "result": updates})


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
def worker(
    bot: FakeBot, engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> TelegramUpdatesWorker:
    return TelegramUpdatesWorker(
        api=bot.api(), engine=engine, session_factory=session_factory, poll_timeout_seconds=0
    )


def start(text: str | None, *, chat_type: str = "private", update_id: int = 1) -> TelegramUpdate:
    return TelegramUpdate(
        update_id=update_id,
        message=TelegramMessage(
            message_id=update_id,
            chat=TelegramChat(id=CHAT_ID, type=chat_type),
            text=text,
        ),
    )


async def issue_code(session: AsyncSession, user_id: UUID, *, now: datetime | None = None) -> str:
    moment = now or datetime.now(UTC)
    issued = await IssueTelegramLinkCode(
        session, bot_username="barber_bot", ttl=timedelta(minutes=15), clock=lambda: moment
    ).execute(user_id)
    return issued.code


async def chat_of(session: AsyncSession, user_id: UUID) -> int | None:
    recipient = await RecipientRepository(session).get(user_id)
    return None if recipient is None else recipient.telegram_chat_id


async def test_a_valid_code_links_the_chat_it_came_from(
    worker: TelegramUpdatesWorker,
    bot: FakeBot,
    session: AsyncSession,
    make_recipient: RecipientFactory,
) -> None:
    recipient = await make_recipient()
    code = await issue_code(session, recipient.user_id)

    await worker.handle(start(f"/start {code}"))

    assert await chat_of(session, recipient.user_id) == CHAT_ID
    assert bot.replies == [(CHAT_ID, LINKED_REPLY)]


async def test_a_chat_can_be_linked_before_the_account_is_known_here(
    worker: TelegramUpdatesWorker, session: AsyncSession
) -> None:
    user_id = uuid4()
    code = await issue_code(session, user_id)

    await worker.handle(start(f"/start {code}"))

    assert await chat_of(session, user_id) == CHAT_ID


async def test_a_code_links_only_once(
    worker: TelegramUpdatesWorker, bot: FakeBot, session: AsyncSession
) -> None:
    code = await issue_code(session, uuid4())
    await worker.handle(start(f"/start {code}"))

    await worker.handle(start(f"/start {code}"))

    assert bot.replies[-1] == (CHAT_ID, INVALID_CODE_REPLY)


async def test_an_expired_code_links_nothing(
    worker: TelegramUpdatesWorker, bot: FakeBot, session: AsyncSession
) -> None:
    user_id = uuid4()
    code = await issue_code(session, user_id, now=datetime.now(UTC) - timedelta(hours=1))

    await worker.handle(start(f"/start {code}"))

    assert await chat_of(session, user_id) is None
    assert bot.replies == [(CHAT_ID, INVALID_CODE_REPLY)]


async def test_an_unknown_code_links_nothing(worker: TelegramUpdatesWorker, bot: FakeBot) -> None:
    await worker.handle(start("/start not-a-code"))

    assert bot.replies == [(CHAT_ID, INVALID_CODE_REPLY)]


async def test_anything_else_gets_a_hint(worker: TelegramUpdatesWorker, bot: FakeBot) -> None:
    await worker.handle(start("hello"))

    assert bot.replies == [(CHAT_ID, HELP_REPLY)]


async def test_a_group_chat_is_never_linked(
    worker: TelegramUpdatesWorker, bot: FakeBot, session: AsyncSession
) -> None:
    user_id = uuid4()
    code = await issue_code(session, user_id)

    await worker.handle(start(f"/start {code}", chat_type="group"))

    assert await chat_of(session, user_id) is None
    assert bot.replies == []


async def test_a_poll_confirms_what_it_handled(worker: TelegramUpdatesWorker, bot: FakeBot) -> None:
    bot.updates = [start("hello", update_id=41).model_dump(mode="json")]

    await worker.run_once()
    await worker.run_once()

    assert bot.offsets == [None, "42"]


async def test_only_one_replica_polls(engine: AsyncEngine) -> None:
    async with _poller_lock(engine) as first, _poller_lock(engine) as second:
        assert (first, second) == (True, False)

    async with _poller_lock(engine) as after_release:
        assert after_release is True
