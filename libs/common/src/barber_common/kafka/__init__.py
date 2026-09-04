"""Kafka: publishing events, consuming them, and what happens when one fails."""

from barber_common.kafka.consumer import (
    EventConsumer,
    EventHandler,
    ProcessingResult,
    RetryPolicy,
)
from barber_common.kafka.dedup import ProcessedEvent, ProcessedEventRepository
from barber_common.kafka.dlq import (
    DLQ_TOPIC_SUFFIX,
    DeadLetter,
    DeadLetterPublisher,
    dlq_topic_of,
)
from barber_common.kafka.producer import EventProducer, MessageHeaders

__all__ = [
    "DLQ_TOPIC_SUFFIX",
    "DeadLetter",
    "DeadLetterPublisher",
    "EventConsumer",
    "EventHandler",
    "EventProducer",
    "MessageHeaders",
    "ProcessedEvent",
    "ProcessedEventRepository",
    "ProcessingResult",
    "RetryPolicy",
    "dlq_topic_of",
]
