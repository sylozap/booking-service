"""Pruning what auth no longer needs.

Three tables grow without bound and nothing else deletes from them: refresh
tokens, email confirmations, and the deduplication table of the consumers.
Left alone they become the reason a database is restored from a backup.

**Rows are deleted by expiry and never by state.** That distinction is the
whole of the correctness of this worker. A refresh token that is revoked but
has not expired is exactly what makes theft detectable: presenting it again is
reuse, and reuse revokes the family (T1.7). Delete it early and the stolen
token stops being reuse and becomes an unknown token -- one 401 instead of a
closed session, and the family the thief also holds stays alive. The same
applies to a spent email confirmation: while its row exists, a second click on
the link is a token that was used, and once it is gone the two are
indistinguishable.

So a row leaves only once it can no longer mean anything, and a retention
window on top of that leaves something to read during an incident.

**Deletion is batched.** One statement that deletes a month of rows holds a
long transaction on a large table and blocks the writes the service is there
to serve. Each pass takes a bounded batch, commits, and comes back.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, CursorResult, delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import InstrumentedAttribute

from barber_auth.models.email_confirmation import EmailConfirmation
from barber_auth.models.refresh_token import RefreshToken
from barber_common.db.base import Base
from barber_common.db.session import unit_of_work
from barber_common.kafka.dedup import ProcessedEvent
from barber_common.logging import get_logger
from barber_common.metrics import counter, gauge

__all__ = ["CleanupWorker", "CleanupSummary"]

_logger = get_logger(__name__)

# Two metrics, and the decision behind each. The moment of the last successful
# pass is what an alert watches: if it stops moving, the tables are growing and
# somebody has to find out why. The counter says whether the passes are
# actually removing anything, which is the difference between "the worker runs"
# and "the worker works". The table is a label because there are three of them
# and they age differently; nothing here is labelled by an identifier.
CLEANUP_LAST_RUN = gauge(
    "auth_cleanup_last_success_timestamp",
    "When the cleanup worker last completed a pass",
)
CLEANUP_DELETED = counter(
    "auth_cleanup_deleted_rows_total",
    "Rows removed by the cleanup worker",
    labelnames=("table",),
)


@dataclass(frozen=True, slots=True)
class CleanupSummary:
    """How much one pass removed, per table."""

    refresh_tokens: int = 0
    email_confirmations: int = 0
    processed_events: int = 0

    @property
    def total(self) -> int:
        return self.refresh_tokens + self.email_confirmations + self.processed_events


class CleanupWorker:
    """Removes expired rows, a bounded batch at a time."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        token_retention_days: int = 7,
        confirmation_retention_days: int = 7,
        processed_event_retention_days: int = 7,
        batch_size: int = 1000,
        interval_seconds: float = 3600.0,
    ) -> None:
        self._session_factory = session_factory
        self._token_retention = timedelta(days=token_retention_days)
        self._confirmation_retention = timedelta(days=confirmation_retention_days)
        self._processed_event_retention = timedelta(days=processed_event_retention_days)
        self._batch_size = batch_size
        self._interval_seconds = interval_seconds

    async def run_once(self, *, now: datetime | None = None) -> CleanupSummary:
        """One pass over the three tables. Returns what it removed.

        Each table gets its own transaction. One transaction for all three
        would hold locks on the refresh tokens while the deduplication table is
        being scanned, and there is no invariant spanning them that would need
        the atomicity.
        """
        moment = now or datetime.now(UTC)

        summary = CleanupSummary(
            refresh_tokens=await self._prune_refresh_tokens(moment),
            email_confirmations=await self._prune_confirmations(moment),
            processed_events=await self._prune_processed_events(moment),
        )

        CLEANUP_LAST_RUN.set(moment.timestamp())
        CLEANUP_DELETED.labels(table="refresh_tokens").inc(summary.refresh_tokens)
        CLEANUP_DELETED.labels(table="email_confirmations").inc(summary.email_confirmations)
        CLEANUP_DELETED.labels(table="processed_events").inc(summary.processed_events)

        if summary.total:
            _logger.info(
                "cleanup pass removed rows",
                refresh_tokens=summary.refresh_tokens,
                email_confirmations=summary.email_confirmations,
                processed_events=summary.processed_events,
            )
        return summary

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Keep pruning until asked to stop.

        Started from the lifespan, so a SIGTERM stops it between passes rather
        than in the middle of one. A pass that fails is logged and retried on
        the next tick: the rows are still there, and an exception here must not
        take the service down with it.
        """
        _logger.info("cleanup worker started", interval_seconds=self._interval_seconds)
        while not stop.is_set():
            try:
                summary = await self.run_once()
            except Exception:
                _logger.exception("cleanup pass failed")
            else:
                # A full batch means there is more waiting. Coming straight
                # back drains a backlog instead of spreading it over hours.
                if summary.total >= self._batch_size:
                    continue

            try:
                async with asyncio.timeout(self._interval_seconds):
                    await stop.wait()
            except TimeoutError:
                continue
        _logger.info("cleanup worker stopped")

    @asynccontextmanager
    async def run_in_background(self) -> AsyncIterator[None]:
        """Run the worker for as long as the block lasts.

        The task is held in a local variable rather than created and forgotten:
        a task nobody references can be collected mid-flight and its exception
        never surfaces.
        """
        stop = asyncio.Event()
        task = asyncio.create_task(self.run_forever(stop), name="auth-cleanup")
        try:
            yield
        finally:
            stop.set()
            await task

    async def _prune_refresh_tokens(self, now: datetime) -> int:
        """Delete tokens that expired longer ago than the retention window.

        By ``expires_at`` and never by ``revoked_at``: a revoked token that has
        not expired yet is what turns a second presentation into detectable
        reuse, and deleting it early would leave the thief's copy of the family
        alive (T1.7).
        """
        cutoff = now - self._token_retention
        return await self._delete_batch(
            model=RefreshToken,
            key=RefreshToken.id,
            condition=RefreshToken.expires_at < cutoff,
        )

    async def _prune_confirmations(self, now: datetime) -> int:
        """Delete confirmations that can no longer confirm anything.

        Expired ones only, spent or not. A spent row is what makes a second
        click on the same link "already used" rather than "never existed", and
        it stops mattering when the link would have expired anyway.
        """
        cutoff = now - self._confirmation_retention
        return await self._delete_batch(
            model=EmailConfirmation,
            key=EmailConfirmation.id,
            condition=EmailConfirmation.expires_at < cutoff,
        )

    async def _prune_processed_events(self, now: datetime) -> int:
        """Delete deduplication rows older than the retention window.

        The row exists to recognise a redelivery. Kafka will not redeliver a
        message a week later, so past that the row protects nothing and only
        costs storage (ADR-0007).
        """
        cutoff = now - self._processed_event_retention
        return await self._delete_batch(
            model=ProcessedEvent,
            key=ProcessedEvent.event_id,
            condition=ProcessedEvent.processed_at < cutoff,
        )

    async def _delete_batch(
        self,
        *,
        model: type[Base],
        key: InstrumentedAttribute[Any],
        condition: ColumnElement[bool],
    ) -> int:
        """Delete at most one batch of matching rows, in one transaction.

        The subquery is what bounds it: ``DELETE ... WHERE key IN (SELECT ...
        LIMIT n)``. A plain ``DELETE ... WHERE`` would take every matching row
        in one statement, and on a table that has been growing for a month
        that is a long transaction blocking the writes this service exists for.
        """
        async with unit_of_work(self._session_factory) as session:
            candidates = select(key).where(condition).limit(self._batch_size).scalar_subquery()
            result = await session.execute(delete(model).where(key.in_(candidates)))
            if not isinstance(result, CursorResult):  # pragma: no cover - DELETE always is one
                raise TypeError("expected a cursor result from a DELETE statement")
            return result.rowcount
