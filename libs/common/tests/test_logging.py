"""The shape of a log record is a contract with Loki, so it is tested."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from barber_common.config import Environment, LogLevel
from barber_common.context import bind_context
from barber_common.logging import configure_logging, get_logger

MANDATORY_FIELDS = (
    "service",
    "env",
    "correlation_id",
    "trace_id",
    "span_id",
    "user_id",
    "level",
    "timestamp",
    "event",
)


def configure() -> None:
    """Bind the log handler to the stdout pytest captures for this test.

    Called from the test body rather than from a fixture on purpose: capsys
    swaps sys.stdout for every phase, and a handler built during setup would
    write into the buffer of the setup phase.
    """
    configure_logging(
        service_name="booking",
        environment=Environment.TEST,
        log_level=LogLevel.INFO,
    )


def read_records(captured: str) -> list[dict[str, object]]:
    """Parse the captured stdout as one JSON document per line."""
    return [json.loads(line) for line in captured.splitlines() if line.strip()]


def test_record_is_json_with_every_mandatory_field(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure()

    get_logger("test").info("booking created")

    records = read_records(capsys.readouterr().out)

    assert len(records) == 1
    assert set(MANDATORY_FIELDS) <= set(records[0])


def test_service_and_environment_come_from_the_configuration(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure()

    get_logger("test").info("booking created")

    record = read_records(capsys.readouterr().out)[0]

    assert record["service"] == "booking"
    assert record["env"] == "test"


def test_context_fields_are_null_outside_a_request(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure()

    get_logger("test").info("worker started")

    record = read_records(capsys.readouterr().out)[0]

    assert record["correlation_id"] is None
    assert record["user_id"] is None


def test_bound_context_reaches_the_record(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure()

    with bind_context(correlation_id="c-1", user_id="u-7"):
        get_logger("test").info("booking cancelled")

    record = read_records(capsys.readouterr().out)[0]

    assert record["correlation_id"] == "c-1"
    assert record["user_id"] == "u-7"


def test_context_is_restored_after_the_block(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure()

    with bind_context(correlation_id="c-1"):
        pass
    get_logger("test").info("after the block")

    record = read_records(capsys.readouterr().out)[0]

    assert record["correlation_id"] is None


async def test_concurrent_tasks_do_not_share_the_correlation_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure()

    async def handle(correlation_id: str) -> None:
        with bind_context(correlation_id=correlation_id):
            await asyncio.sleep(0)  # force the tasks to interleave
            get_logger("test").info(correlation_id)

    await asyncio.gather(handle("c-1"), handle("c-2"), handle("c-3"))

    records = read_records(capsys.readouterr().out)

    assert len(records) == 3
    assert all(record["event"] == record["correlation_id"] for record in records)


def test_records_of_third_party_loggers_are_json_too(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure()

    logging.getLogger("sqlalchemy.engine").warning("pool is exhausted")

    record = read_records(capsys.readouterr().out)[0]

    assert record["service"] == "booking"
    assert record["event"] == "pool is exhausted"
