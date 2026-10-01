"""Domain metrics of the booking service.

Each one exists because a change in it leads to a decision:

* ``bookings_created_total`` is the business pulse -- a salon that stops
  appearing here has a broken schedule or a deactivated master;
* ``booking_conflicts_total`` counts lost races, which is how stale the
  availability clients are shown actually is;
* ``availability_query_duration_seconds`` watches the heaviest query of the
  platform, and its p95 is what the SLO is written against;
* ``reminder_scheduler_last_run_timestamp`` stops moving when the scheduler
  dies quietly, which nothing else would show until clients miss reminders.

The salon is a label and the master is not: there are twenty salons and two
hundred masters, and an identifier in a label is how a metric turns into a
memory leak.
"""

from __future__ import annotations

from barber_common.metrics import counter, gauge, histogram

__all__ = [
    "AVAILABILITY_DURATION",
    "BOOKINGS_CREATED",
    "BOOKING_CONFLICTS",
    "REMINDER_SCHEDULER_LAST_RUN",
    "AvailabilitySource",
]

BOOKINGS_CREATED = counter(
    "bookings_created_total",
    "Bookings created, by salon and status",
    labelnames=("salon", "status"),
)

BOOKING_CONFLICTS = counter(
    "booking_conflicts_total",
    "Bookings refused because the slot was taken while the request was in flight",
    labelnames=("salon",),
)

# Labelled by where the answer came from: a p95 that mixes cache hits with
# database reads hides the query it is supposed to watch.
AVAILABILITY_DURATION = histogram(
    "availability_query_duration_seconds",
    "Time to answer one availability request",
    labelnames=("source",),
)


# Unix time of the last pass that committed. The alert is on its age.
REMINDER_SCHEDULER_LAST_RUN = gauge(
    "reminder_scheduler_last_run_timestamp",
    "When the reminder scheduler last finished a pass, as Unix time",
)


class AvailabilitySource:
    """Label values of :data:`AVAILABILITY_DURATION`."""

    CACHE = "cache"
    DATABASE = "database"
