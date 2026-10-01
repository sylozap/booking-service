"""Domain metrics of the notification service.

Each one exists because a change in it leads to a decision:

* ``notifications_sent_total{channel,result}`` is deliverability: a channel
  whose failures grow has a broken provider -- a revoked bot token, a mail
  relay that is down -- and its temporary failures turn into dead letters soon;
* ``notification_send_duration_seconds{channel}`` says whether a provider got
  slow before its timeouts start failing deliveries;
* ``notifications_pending_messages`` and ``notifications_oldest_due_age_seconds``
  watch the queue from two sides, as the outbox gauges do: a count alone hides
  one stuck row, an age alone hides a backlog. The age counts only rows that
  are due -- a retry scheduled for later is not late.

The channel and the outcome are labels; the user, the template and the event
are not. There are two channels and three outcomes, and an identifier in a
label is how a metric turns into a memory leak.
"""

from __future__ import annotations

from barber_common.metrics import counter, gauge, histogram

__all__ = [
    "NOTIFICATIONS_OLDEST_DUE_AGE",
    "NOTIFICATIONS_PENDING",
    "NOTIFICATIONS_SENT",
    "NOTIFICATION_SEND_DURATION",
]

# One per attempt, whatever its outcome; ``result`` is a DeliveryOutcome.
NOTIFICATIONS_SENT = counter(
    "notifications_sent_total",
    "Attempts to deliver a notification, by channel and outcome",
    labelnames=("channel", "result"),
)

# Only the call to the provider: a notification refused before it -- no
# address, a deactivated recipient -- took no time worth measuring.
NOTIFICATION_SEND_DURATION = histogram(
    "notification_send_duration_seconds",
    "Time a provider took to deliver or refuse one notification",
    labelnames=("channel",),
)

NOTIFICATIONS_PENDING = gauge(
    "notifications_pending_messages",
    "Notifications queued and not yet sent or given up on",
)

NOTIFICATIONS_OLDEST_DUE_AGE = gauge(
    "notifications_oldest_due_age_seconds",
    "How long the oldest notification due for sending has waited for a worker",
)
