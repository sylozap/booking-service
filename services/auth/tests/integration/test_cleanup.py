"""The cleanup worker: what it removes, and what it must never touch.

The must-never half is the important one. A refresh token that is revoked but
has not expired is what turns a stolen token presented a second time into
detectable reuse; deleting it early would quietly downgrade theft detection to
an ordinary 401 and leave the thief's family alive.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from barber_auth.domain.confirmation import hash_confirmation_token
from barber_auth.domain.tokens import hash_refresh_token
from barber_auth.models.email_confirmation import EmailConfirmation
from barber_auth.models.refresh_token import RefreshToken
from barber_auth.models.user import User
from barber_auth.workers.cleanup import CleanupWorker
from barber_common.kafka.dedup import ProcessedEvent
from barber_common.metrics import REGISTRY

pytestmark = pytest.mark.integration

LAST_RUN_METRIC = "auth_cleanup_last_success_timestamp"
DELETED_METRIC = "auth_cleanup_deleted_rows_total"

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
RETENTION_DAYS = 7

UserFactory = Callable[..., Awaitable[User]]


@pytest.fixture
def worker(session_factory: async_sessionmaker[AsyncSession]) -> CleanupWorker:
    return CleanupWorker(
        session_factory=session_factory,
        token_retention_days=RETENTION_DAYS,
        confirmation_retention_days=RETENTION_DAYS,
        processed_event_retention_days=RETENTION_DAYS,
        batch_size=100,
    )


def add_token(
    session: AsyncSession,
    user: User,
    *,
    expires_at: datetime,
    revoked_at: datetime | None = None,
) -> RefreshToken:
    token = RefreshToken(
        user_id=user.id,
        family_id=uuid4(),
        token_hash=hash_refresh_token(uuid4().hex),
        expires_at=expires_at,
        revoked_at=revoked_at,
    )
    session.add(token)
    return token


def add_confirmation(
    session: AsyncSession,
    user: User,
    *,
    expires_at: datetime,
    used_at: datetime | None = None,
) -> EmailConfirmation:
    confirmation = EmailConfirmation(
        user_id=user.id,
        token_hash=hash_confirmation_token(uuid4().hex),
        expires_at=expires_at,
        used_at=used_at,
    )
    session.add(confirmation)
    return confirmation


async def test_a_long_expired_token_is_removed(
    worker: CleanupWorker, session: AsyncSession, make_user: UserFactory
) -> None:
    user = await make_user()
    add_token(session, user, expires_at=NOW - timedelta(days=RETENTION_DAYS + 1))
    await session.commit()

    summary = await worker.run_once(now=NOW)

    assert summary.refresh_tokens == 1
    assert (await session.execute(select(RefreshToken))).scalars().all() == []


async def test_a_revoked_token_that_has_not_expired_is_kept(
    worker: CleanupWorker, session: AsyncSession, make_user: UserFactory
) -> None:
    user = await make_user()
    add_token(
        session,
        user,
        expires_at=NOW + timedelta(days=20),
        revoked_at=NOW - timedelta(days=30),
    )
    await session.commit()

    await worker.run_once(now=NOW)

    # A revoked but unexpired token must survive, so presenting it again is
    # still detected as reuse.
    assert len((await session.execute(select(RefreshToken))).scalars().all()) == 1


async def test_a_live_token_is_kept(
    worker: CleanupWorker, session: AsyncSession, make_user: UserFactory
) -> None:
    user = await make_user()
    add_token(session, user, expires_at=NOW + timedelta(days=30))
    await session.commit()

    await worker.run_once(now=NOW)

    assert len((await session.execute(select(RefreshToken))).scalars().all()) == 1


async def test_a_token_inside_the_retention_window_is_kept(
    worker: CleanupWorker, session: AsyncSession, make_user: UserFactory
) -> None:
    user = await make_user()
    add_token(session, user, expires_at=NOW - timedelta(days=RETENTION_DAYS - 1))
    await session.commit()

    await worker.run_once(now=NOW)

    # Expired, but recently enough that an incident from this week can still
    # be read.
    assert len((await session.execute(select(RefreshToken))).scalars().all()) == 1


async def test_an_expired_confirmation_is_removed(
    worker: CleanupWorker, session: AsyncSession, make_user: UserFactory
) -> None:
    user = await make_user()
    add_confirmation(session, user, expires_at=NOW - timedelta(days=RETENTION_DAYS + 1))
    await session.commit()

    summary = await worker.run_once(now=NOW)

    assert summary.email_confirmations == 1


async def test_a_spent_confirmation_that_has_not_expired_is_kept(
    worker: CleanupWorker, session: AsyncSession, make_user: UserFactory
) -> None:
    user = await make_user()
    add_confirmation(
        session, user, expires_at=NOW + timedelta(hours=12), used_at=NOW - timedelta(hours=1)
    )
    await session.commit()

    await worker.run_once(now=NOW)

    # While the row exists, a second click on the link is "already used"
    # rather than "never existed".
    assert len((await session.execute(select(EmailConfirmation))).scalars().all()) == 1


async def test_an_old_deduplication_row_is_removed(
    worker: CleanupWorker, session: AsyncSession
) -> None:
    session.add(
        ProcessedEvent(
            event_id=uuid4(),
            consumer_group="auth",
            processed_at=NOW - timedelta(days=RETENTION_DAYS + 1),
        )
    )
    await session.commit()

    summary = await worker.run_once(now=NOW)

    # Kafka does not redeliver a message a week later, so the row is no longer
    # needed.
    assert summary.processed_events == 1


async def test_a_recent_deduplication_row_is_kept(
    worker: CleanupWorker, session: AsyncSession
) -> None:
    session.add(
        ProcessedEvent(
            event_id=uuid4(), consumer_group="auth", processed_at=NOW - timedelta(hours=1)
        )
    )
    await session.commit()

    await worker.run_once(now=NOW)

    assert len((await session.execute(select(ProcessedEvent))).scalars().all()) == 1


async def test_a_pass_removes_at_most_one_batch(
    session_factory: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    make_user: UserFactory,
) -> None:
    user = await make_user()
    for _ in range(5):
        add_token(session, user, expires_at=NOW - timedelta(days=RETENTION_DAYS + 1))
    await session.commit()
    worker = CleanupWorker(session_factory=session_factory, batch_size=2)

    summary = await worker.run_once(now=NOW)

    # A statement that deletes a month of rows at once holds a long
    # transaction on a large table and blocks the writes the service exists
    # for. Each pass takes a bounded batch and comes back.
    assert summary.refresh_tokens == 2
    assert len((await session.execute(select(RefreshToken))).scalars().all()) == 3


async def test_repeated_passes_drain_the_backlog(
    session_factory: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    make_user: UserFactory,
) -> None:
    user = await make_user()
    for _ in range(5):
        add_token(session, user, expires_at=NOW - timedelta(days=RETENTION_DAYS + 1))
    await session.commit()
    worker = CleanupWorker(session_factory=session_factory, batch_size=2)

    for _ in range(3):
        await worker.run_once(now=NOW)

    assert (await session.execute(select(RefreshToken))).scalars().all() == []


async def test_a_pass_over_empty_tables_removes_nothing(worker: CleanupWorker) -> None:
    summary = await worker.run_once(now=NOW)

    assert summary.total == 0


async def test_a_second_pass_is_a_no_op(
    worker: CleanupWorker, session: AsyncSession, make_user: UserFactory
) -> None:
    user = await make_user()
    add_token(session, user, expires_at=NOW - timedelta(days=RETENTION_DAYS + 1))
    await session.commit()

    await worker.run_once(now=NOW)
    second = await worker.run_once(now=NOW)

    # Restartable by construction: interrupting the worker at any moment
    # leaves nothing for the next pass to get wrong.
    assert second.total == 0


async def test_the_metric_of_the_last_pass_moves(worker: CleanupWorker) -> None:
    await worker.run_once(now=NOW)

    # What an alert watches: if this stops moving, the tables are growing and
    # somebody has to find out why.
    assert REGISTRY.get_sample_value(LAST_RUN_METRIC) == NOW.timestamp()


async def test_deleted_rows_are_counted_per_table(
    worker: CleanupWorker, session: AsyncSession, make_user: UserFactory
) -> None:
    user = await make_user()
    add_token(session, user, expires_at=NOW - timedelta(days=RETENTION_DAYS + 1))
    await session.commit()
    before = REGISTRY.get_sample_value(DELETED_METRIC, {"table": "refresh_tokens"}) or 0.0

    await worker.run_once(now=NOW)

    # The difference between "the worker runs" and "the worker works".
    after = REGISTRY.get_sample_value(DELETED_METRIC, {"table": "refresh_tokens"})
    assert after == before + 1


async def test_the_background_task_runs_a_pass_and_stops_when_asked(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = CleanupWorker(session_factory=session_factory, interval_seconds=3600.0)
    # Compared for a change and not for growth: this gauge belongs to the
    # process, and the tests above set it to a fixed moment that may be ahead
    # of the real clock. "Greater than" would then never come true and the
    # failure would look like the worker never running.
    before = REGISTRY.get_sample_value(LAST_RUN_METRIC)

    # The timeout is the assertion about stopping: with an hourly interval, a
    # worker that only woke up on the timer would hang here instead of leaving
    # the block. A SIGTERM has to stop it between passes, not in an hour.
    async with asyncio.timeout(5), worker.run_in_background():
        await _wait_for_a_pass(before)

    recorded = REGISTRY.get_sample_value(LAST_RUN_METRIC)
    assert recorded is not None
    assert abs(recorded - datetime.now(UTC).timestamp()) < 60


async def _wait_for_a_pass(before: float | None) -> None:
    """Poll until the worker records a pass, rather than sleeping for one."""
    # ASYNC110 suggests waiting on an Event. There is none: the change happens
    # inside the background task, and polling is how a test sees it.
    while REGISTRY.get_sample_value(LAST_RUN_METRIC) == before:  # noqa: ASYNC110
        await asyncio.sleep(0.01)
