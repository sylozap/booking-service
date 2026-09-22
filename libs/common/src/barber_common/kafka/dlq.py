"""The dead letter queue: where a message goes when handling it cannot work.

Two failures end up here and they are not the same. A message that does not
parse is hopeless: a retry runs the same code over the same bytes and fails the
same way, so it goes to the DLQ at once. A handler that failed on a database
that was down is worth retrying, and only after the attempts are spent does the
message go to the DLQ.

Either way the offset is committed. A poison message that stops the partition
takes every later message with it, which turns one broken event into an outage
of the whole topic.

The message keeps its original envelope and gains the fields that say what
happened, so the manual review has the payload and the reason in one place.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Self

from aiokafka import AIOKafkaProducer

from barber_common.kafka.producer import MessageHeaders
from barber_common.logging import get_logger
from barber_common.metrics import counter

__all__ = ["DLQ_TOPIC_SUFFIX", "DeadLetter", "DeadLetterPublisher", "dlq_topic_of"]

DLQ_TOPIC_SUFFIX = ".dlq"

_logger = get_logger(__name__)

# Any growth is an incident: every message here is an event the platform
# accepted and then failed to act on.
DLQ_MESSAGES = counter(
    "dlq_messages_total",
    "Messages moved to a dead letter topic",
    labelnames=("topic", "reason"),
)


def dlq_topic_of(topic: str) -> str:
    """Name of the dead letter topic of a topic."""
    return f"{topic}{DLQ_TOPIC_SUFFIX}"


@dataclass(frozen=True, slots=True)
class DeadLetter:
    """One message that could not be handled, with the reason why.

    ``reason`` is a short class of failure -- ``invalid_message``,
    ``handler_failed`` -- and is a metric label, so it stays a fixed set of
    values. The unbounded description belongs in ``detail``.
    """

    original_topic: str
    reason: str
    detail: str
    attempts: int
    value: bytes
    key: bytes | None = None
    headers: MessageHeaders | None = None
    traceback: str | None = None


class DeadLetterPublisher:
    """Sends the messages a consumer gave up on.

    It talks to the broker directly instead of going through the outbox, and
    that is not the exception the outbox rule forbids: a dead letter is not a
    domain event of this service. It carries no state change, nothing is
    atomic with it, and it exists precisely because the transaction that would
    have owned it never happened.
    """

    def __init__(
        self,
        *,
        bootstrap_servers: str,
        client: AIOKafkaProducer | None = None,
    ) -> None:
        self._client = client or AIOKafkaProducer(
            bootstrap_servers=bootstrap_servers,
            acks="all",
            enable_idempotence=True,
        )
        self._is_started = False

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.stop()

    async def start(self) -> None:
        if self._is_started:
            return
        await self._client.start()
        self._is_started = True

    async def stop(self) -> None:
        if not self._is_started:
            return
        await self._client.stop()
        self._is_started = False

    async def publish(self, letter: DeadLetter) -> None:
        """Send one dead letter and wait for the acknowledgement.

        The key is kept as it was: the messages of one aggregate stay in one
        partition of the dead letter topic too, which is what makes reading
        them in order during a review possible.
        """
        # Connected on first use, like the relay: a broker that is down at
        # startup must not stop the service. The call is idempotent.
        await self.start()
        topic = dlq_topic_of(letter.original_topic)
        await self._client.send_and_wait(
            topic,
            key=letter.key,
            value=_build_body(letter),
            headers=_build_headers(letter),
        )

        DLQ_MESSAGES.labels(topic=letter.original_topic, reason=letter.reason).inc()
        _logger.error(
            "message moved to the dead letter topic",
            topic=letter.original_topic,
            dlq_topic=topic,
            dlq_reason=letter.reason,
            dlq_detail=letter.detail,
            dlq_attempts=letter.attempts,
        )


def _build_body(letter: DeadLetter) -> bytes:
    """Add the diagnosis to the original envelope.

    When the envelope does not parse -- the reason the message is here in the
    first half of the cases -- the raw bytes are kept as text instead, because
    a review with no payload has nothing to review.
    """
    document: dict[str, object] = {
        "dlq_reason": letter.reason,
        "dlq_detail": letter.detail,
        "dlq_traceback": letter.traceback,
        "dlq_attempts": letter.attempts,
        "dlq_original_topic": letter.original_topic,
        "dlq_at": datetime.now(UTC).isoformat(),
    }
    document.update(_original_envelope(letter.value))
    return json.dumps(document).encode("utf-8")


def _original_envelope(value: bytes) -> dict[str, object]:
    try:
        envelope = json.loads(value)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"dlq_original_value": value.decode("utf-8", errors="replace")}

    if not isinstance(envelope, dict):
        return {"dlq_original_value": envelope}
    return envelope


def _build_headers(letter: DeadLetter) -> MessageHeaders:
    """Keep the original headers and add the reason next to them.

    The reason in a header is what lets an operator filter a dead letter topic
    without deserialising every message in it.
    """
    headers: MessageHeaders = list(letter.headers or [])
    headers.append(("dlq_reason", letter.reason.encode("utf-8")))
    headers.append(("dlq_original_topic", letter.original_topic.encode("utf-8")))
    return headers
