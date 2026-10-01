"""Entry point of the notification service.

Delivery of notifications: the log and Telegram adapters, retries. Keeps its
own copy of contacts, filled from ``auth.users.v1``; queues a message for every
booking event worth one, and a worker sends what is queued.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI

from barber_common.app import create_app, use_database
from barber_common.auth import jwks_verifier, refreshing, use_authentication
from barber_common.db import Database, check_schema_is_current, load_config
from barber_common.db.engine import create_engine_from_settings
from barber_common.kafka import DeadLetterPublisher, EventConsumer, EventProducer
from barber_common.outbox import OutboxRelay
from barber_notification.api.v1.router import router
from barber_notification.consumers.booking_events import (
    BOOKING_EVENTS_GROUP,
    BOOKING_EVENTS_TOPICS,
    BookingEvents,
)
from barber_notification.consumers.user_events import (
    USER_EVENTS_GROUP,
    USER_EVENTS_TOPICS,
    UserEvents,
)
from barber_notification.providers.registry import ProviderRegistry
from barber_notification.providers.telegram import TelegramBotApi, TelegramProvider
from barber_notification.settings import ALEMBIC_INI, NotificationSettings
from barber_notification.workers.delivery import DeliveryWorker
from barber_notification.workers.telegram_updates import TelegramUpdatesWorker

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
            handlers=UserEvents(confirmation_url=resolved.email_confirmation_url).handlers(),
        )
        # A group of its own: a burst of bookings must not hold back the
        # contacts the messages about them are sent to.
        booking_events = EventConsumer(
            topics=BOOKING_EVENTS_TOPICS,
            group_id=BOOKING_EVENTS_GROUP,
            bootstrap_servers=resolved.kafka_bootstrap_servers,
            session_factory=database.session_factory,
            dead_letters=dead_letters,
            handlers=BookingEvents().handlers(),
        )

        # Every service checks the tokens it receives itself, against the
        # public keys of auth, not against a header the gateway set.
        jwks, verifier = jwks_verifier(resolved)

        telegram = _build_telegram(resolved)
        delivery = DeliveryWorker(
            session_factory=database.session_factory,
            providers=ProviderRegistry.build(
                environment=resolved.environment,
                telegram=TelegramProvider(telegram) if telegram is not None else None,
            ),
            batch_size=resolved.delivery_batch_size,
            lease=timedelta(seconds=resolved.delivery_lease_seconds),
            max_attempts=resolved.delivery_max_attempts,
            retry_delay=timedelta(seconds=resolved.delivery_retry_delay_seconds),
            idle_interval_seconds=resolved.delivery_interval_seconds,
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
            stack.push_async_callback(booking_events.stop)
            await stack.enter_async_context(booking_events.run_in_background())
            # Keys are fetched lazily, so auth being down delays the first
            # verification instead of stopping the start.
            await stack.enter_async_context(refreshing(jwks))
            use_authentication(app, verifier)
            if telegram is not None:
                stack.push_async_callback(telegram.aclose)
                updates = TelegramUpdatesWorker(
                    api=telegram,
                    engine=database.engine,
                    session_factory=database.session_factory,
                    poll_timeout_seconds=resolved.telegram_poll_timeout_seconds,
                )
                await stack.enter_async_context(updates.run_in_background())
            await stack.enter_async_context(delivery.run_in_background())
            yield

    return create_app(resolved, routers=[router], lifespan=lifespan, title="Barber Notification")


def _build_telegram(settings: NotificationSettings) -> TelegramBotApi | None:
    """The Bot API client, or nothing when no bot is configured.

    Without a token the service still starts: the telegram channel goes to the
    log and linking a chat is refused.
    """
    if not settings.telegram_enabled or settings.telegram_bot_token is None:
        return None
    return TelegramBotApi(
        token=settings.telegram_bot_token,
        base_url=settings.telegram_api_url,
        timeout_seconds=settings.telegram_timeout_seconds,
    )
