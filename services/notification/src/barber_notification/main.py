"""Entry point of the notification service.

Delivery of notifications: the log and Telegram adapters, retries. Keeps its
own copy of contacts, filled from ``auth.users.v1``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from barber_common.app import create_app, use_database
from barber_common.db import Database, check_schema_is_current, load_config
from barber_common.db.engine import create_engine_from_settings
from barber_common.kafka import DeadLetterPublisher, EventConsumer, EventProducer
from barber_common.outbox import OutboxRelay
from barber_notification.api.v1.router import router
from barber_notification.consumers.user_events import (
    USER_EVENTS_GROUP,
    USER_EVENTS_TOPICS,
    UserEvents,
)
from barber_notification.settings import ALEMBIC_INI, NotificationSettings

__all__ = ["create_application"]


def create_application(settings: NotificationSettings | None = None) -> FastAPI:
    """Build the application. Called by uvicorn with ``--factory``.

    ``settings`` is an argument so a test can assemble the service without an
    environment behind it.
    """
    resolved = settings or NotificationSettings.load()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = Database(create_engine_from_settings(resolved))
        # Before a single request is served: a pod working against a schema of
        # the wrong version writes rows the next release cannot read.
        await check_schema_is_current(database.engine, load_config(ALEMBIC_INI))
        use_database(app, database)

        producer = EventProducer(
            bootstrap_servers=resolved.kafka_bootstrap_servers,
            service_name=resolved.service_name,
        )
        relay = OutboxRelay(session_factory=database.session_factory, producer=producer)

        dead_letters = DeadLetterPublisher(bootstrap_servers=resolved.kafka_bootstrap_servers)
        user_events = EventConsumer(
            topics=USER_EVENTS_TOPICS,
            group_id=USER_EVENTS_GROUP,
            bootstrap_servers=resolved.kafka_bootstrap_servers,
            session_factory=database.session_factory,
            dead_letters=dead_letters,
            handlers=UserEvents().handlers(),
        )

        async with AsyncExitStack() as stack:
            # The producer is not started here: the relay connects on its first
            # pass, so a broker that is down delays events instead of stopping
            # the service.
            stack.push_async_callback(producer.stop)
            await stack.enter_async_context(relay.run_in_background())
            # Like the relay, the consumer joins its group on the first pass.
            stack.push_async_callback(dead_letters.stop)
            stack.push_async_callback(user_events.stop)
            await stack.enter_async_context(user_events.run_in_background())
            yield

    return create_app(resolved, routers=[router], lifespan=lifespan, title="Barber Notification")
