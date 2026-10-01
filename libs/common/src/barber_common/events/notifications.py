"""Payloads ``notification`` writes to the dead letter topics.

A notification the delivery worker gave up on is published to ``<topic>.dlq``
of the event that caused it, so the review of ``booking.bookings.v1.dlq`` finds
the messages the platform failed to send next to the events it failed to handle.

It goes through the outbox, unlike a dead letter of the consumer runner, because
here there is a state change to be atomic with: the notification turning
``failed``. A pod that died between the two would lose the record otherwise.

The relay wraps it in an envelope of its own, so the original envelope is not
kept; ``original_event_id`` -- also the ``causation_id`` of the envelope -- is
how the review finds it. The ``dlq_*`` fields are named as in a dead letter of
the runner, so both read the same way.

The fields of the template are left out on purpose: a confirmation letter holds
a one-time token in them, and a dead letter topic is read by people.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict

__all__ = [
    "NOTIFICATION_AGGREGATE_TYPE",
    "DeliveryFailureReason",
    "NotificationDeliveryFailed",
    "NotificationEventType",
]

# The partitioning key: the notification itself. The booking or the account
# behind it is not known here, and dead letters need no order between them.
NOTIFICATION_AGGREGATE_TYPE = "notifications"


class NotificationEventType(StrEnum):
    """``event_type`` of the envelope."""

    DELIVERY_FAILED = "notification.delivery_failed"


class DeliveryFailureReason(StrEnum):
    """Why the worker gave up. Also the ``reason`` label of ``dlq_messages_total``."""

    # Every attempt failed with an error worth retrying.
    RETRIES_EXHAUSTED = "retries_exhausted"
    # Another attempt would fail the same way: the bot is blocked, there is no
    # address, the recipient is deactivated.
    PERMANENT_FAILURE = "permanent_failure"


class NotificationDeliveryFailed(BaseModel):
    """A notification that will not be sent, and why."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    notification_id: UUID
    original_event_id: UUID
    user_id: UUID
    channel: str
    template: str

    dlq_reason: DeliveryFailureReason
    dlq_detail: str
    dlq_attempts: int
    dlq_original_topic: str
    dlq_at: AwareDatetime
