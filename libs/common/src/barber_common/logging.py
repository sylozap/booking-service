"""Structured logging: JSON to stdout, one format for the whole platform.

The shape of a record is fixed here and does not change afterwards: Loki
queries and Grafana dashboards are built on it. Every record carries
``service``, ``env`` and the request context fields, whether or not a request
is in progress.

Third-party loggers (uvicorn, SQLAlchemy, aiokafka) go through the same
formatter, so the output of a pod is JSON on every line.
"""

from __future__ import annotations

import logging
import sys

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

from barber_common.config import Environment, LogLevel
from barber_common.context import current_context

__all__ = ["configure_logging", "get_logger"]


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return the logger for a module.

    ``print`` is forbidden in the project; this is the only way out to stdout.
    """
    return structlog.stdlib.get_logger(name)


def _add_request_context(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """Mix the correlation, trace and user identifiers into the record."""
    for field, value in current_context().items():
        event_dict.setdefault(field, value)
    return event_dict


def _service_context(service_name: str, environment: Environment) -> Processor:
    """Build a processor stamping the service identity onto every record."""

    def add_service_context(
        logger: WrappedLogger, method_name: str, event_dict: EventDict
    ) -> EventDict:
        event_dict.setdefault("service", service_name)
        event_dict.setdefault("env", environment.value)
        return event_dict

    return add_service_context


def configure_logging(
    *,
    service_name: str,
    environment: Environment,
    log_level: LogLevel = LogLevel.INFO,
) -> None:
    """Configure structlog and the standard library logging as one pipeline.

    Idempotent: calling it twice replaces the previous configuration instead of
    stacking a second handler, which would double every line.
    """
    shared_processors: list[Processor] = [
        _service_context(service_name, environment),
        _add_request_context,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(sort_keys=True),
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(log_level.value)
