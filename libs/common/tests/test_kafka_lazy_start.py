"""The consumer and the dead letter publisher connect on first use."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from barber_common.kafka.consumer import EventConsumer
from barber_common.kafka.dlq import DeadLetterPublisher
from barber_common.testing.factories import dead_letter_factory


def broker_client() -> MagicMock:
    """An aiokafka client that connects and returns nothing."""
    client = MagicMock()
    client.start = AsyncMock()
    client.getmany = AsyncMock(return_value={})
    client.send_and_wait = AsyncMock()
    client.assignment.return_value = set()
    return client


async def test_the_consumer_joins_the_group_on_its_first_pass() -> None:
    client = broker_client()
    consumer = EventConsumer(
        topics=["test.events.v1"],
        group_id="test-group",
        session_factory=MagicMock(),
        dead_letters=MagicMock(),
        client=client,
    )

    await consumer.run_once()
    await consumer.run_once()

    client.start.assert_awaited_once()


async def test_the_dead_letter_publisher_connects_on_its_first_letter() -> None:
    client = broker_client()
    publisher = DeadLetterPublisher(bootstrap_servers="", client=client)

    await publisher.publish(dead_letter_factory())
    await publisher.publish(dead_letter_factory())

    client.start.assert_awaited_once()
    assert client.send_and_wait.await_count == 2
