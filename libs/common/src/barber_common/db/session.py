"""Sessions, transactions and the unit of work.

Transaction boundaries belong to the scenario layer. A repository never
commits: otherwise the row, the outbox record and the idempotency key cannot be
written inside one transaction.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from starlette.requests import Request

__all__ = [
    "Database",
    "after_commit",
    "create_session_factory",
    "get_database",
    "get_session",
    "transaction",
    "unit_of_work",
]


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build the session factory of a service.

    ``expire_on_commit=False`` keeps loaded attributes readable after a commit;
    without it, mapping an entity after the transaction closed triggers a
    refresh against a session that is already gone.
    """
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


@asynccontextmanager
async def transaction(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    """Run a block inside a transaction, joining the one already open.

    Nesting does not start a second transaction: a scenario that calls another
    scenario must stay atomic, and a nested ``begin()`` would commit half of the
    work on the inner exit.
    """
    if session.in_transaction():
        yield session
        return

    async with session.begin():
        yield session


_PENDING_KEY = "barber_after_commit"


def after_commit(session: AsyncSession, callback: Callable[[], None]) -> None:
    """Run ``callback`` once the transaction the session is in commits.

    For what must count only work that happened: a counter moved inside a
    transaction that then rolls back -- a consumer retrying a handler -- would
    count the same thing twice. On a rollback the callback is dropped.

    The scenario may not own the transaction it runs in, so "after the block"
    is not always "after the commit"; this is.
    """
    pending: list[Callable[[], None]] | None = session.info.get(_PENDING_KEY)
    if pending is None:
        pending = []
        session.info[_PENDING_KEY] = pending
        sync_session = session.sync_session

        @event.listens_for(sync_session, "after_commit")
        def run_pending(_: object) -> None:
            callbacks = list(pending)
            pending.clear()
            for queued in callbacks:
                queued()

        @event.listens_for(sync_session, "after_rollback")
        def drop_pending(_: object) -> None:
            pending.clear()

    pending.append(callback)


@asynccontextmanager
async def unit_of_work(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Open a session and a transaction around one piece of work.

    Used by consumers and background workers, where no HTTP dependency runs.
    The transaction commits on a clean exit and rolls back on any exception.
    """
    async with session_factory() as session, transaction(session):
        yield session


class Database:
    """Owner of the engine and the session factory of one service.

    Lives in ``app.state`` rather than in a module level variable: a global
    engine makes tests share a pool and depend on their order.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session_factory = create_session_factory(engine)

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory

    def unit_of_work(self) -> AbstractAsyncContextManager[AsyncSession]:
        """Shorthand for :func:`unit_of_work` bound to this session factory."""
        return unit_of_work(self._session_factory)

    async def check_connection(self) -> None:
        """Raise if the database does not answer. Used by the readiness probe."""
        async with self._engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def dispose(self) -> None:
        """Close every pooled connection. Called from the lifespan shutdown."""
        await self._engine.dispose()


def get_database(request: Request) -> Database:
    """Return the database of the running application."""
    database = getattr(request.app.state, "database", None)
    if not isinstance(database, Database):
        raise RuntimeError(
            "application state has no database: "
            "assign app.state.database in the lifespan before serving requests"
        )
    return database


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency handing a session to the router.

    The session arrives without an open transaction: the scenario decides where
    the transaction starts and ends.
    """
    database = get_database(request)
    async with database.session_factory() as session:
        yield session
