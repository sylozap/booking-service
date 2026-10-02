"""Test infrastructure shared by every service.

Imported only from ``conftest.py`` files. It lives in the shipped package
rather than in a test directory because five services need the same containers,
the same isolation and the same waiting helpers, and a copy per service drifts
within a month.
"""

from barber_common.testing.factories import (
    dead_letter_factory,
    envelope_factory,
    outbox_message_factory,
)
from barber_common.testing.fixtures import (
    app_client,
    apply_migrations,
    create_database,
    docker_is_available,
    isolated_session_factory,
    read_events,
    wait_for,
)
from barber_common.testing.tracing import recorded_spans

__all__ = [
    "app_client",
    "apply_migrations",
    "create_database",
    "dead_letter_factory",
    "docker_is_available",
    "envelope_factory",
    "isolated_session_factory",
    "outbox_message_factory",
    "read_events",
    "recorded_spans",
    "wait_for",
]
