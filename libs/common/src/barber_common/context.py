"""Per-request context carried through the call stack.

The values live in :mod:`contextvars`, so they follow ``await`` boundaries and
stay isolated between concurrent tasks. Nothing passes them by hand: the log
processor reads them, the HTTP client and the Kafka producer copy them into
outgoing headers, and the consumer restores them from an incoming message.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from uuid import UUID

from opentelemetry import trace

__all__ = [
    "CONTEXT_FIELDS",
    "bind_causation",
    "bind_context",
    "current_context",
    "get_causation_id",
    "get_correlation_id",
    "get_user_id",
    "set_trace_context",
]

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)
_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)
_span_id: ContextVar[str | None] = ContextVar("span_id", default=None)
_user_id: ContextVar[str | None] = ContextVar("user_id", default=None)
# Not a log field: the format of a record is fixed, and the event being handled
# is already named in the records of the consumer.
_causation_id: ContextVar[UUID | None] = ContextVar("causation_id", default=None)

# Order matters only for readability of the log output.
CONTEXT_FIELDS = ("correlation_id", "trace_id", "span_id", "user_id")

_VARIABLES: dict[str, ContextVar[str | None]] = {
    "correlation_id": _correlation_id,
    "trace_id": _trace_id,
    "span_id": _span_id,
    "user_id": _user_id,
}


def get_correlation_id() -> str | None:
    """Return the correlation id of the current request, if one is set."""
    return _correlation_id.get()


def get_user_id() -> str | None:
    """Return the id of the authenticated user, if the request has one."""
    return _user_id.get()


def get_causation_id() -> UUID | None:
    """Return the id of the event being handled, if this code runs because of one."""
    return _causation_id.get()


def set_trace_context(*, trace_id: str | None, span_id: str | None) -> None:
    """Publish the current span into the logging context.

    Called by the tracing integration on every span start; unlike
    :func:`bind_context` it does not restore the previous value, because the
    span that follows overwrites it anyway.
    """
    _trace_id.set(trace_id)
    _span_id.set(span_id)


def _current_span_context() -> tuple[str, str] | None:
    """Return the ids of the span being executed, if tracing is recording.

    While the exporter is off, OpenTelemetry hands out a non-recording span with
    an invalid context, and the log fields stay ``null``.
    """
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None
    return (
        trace.format_trace_id(span_context.trace_id),
        trace.format_span_id(span_context.span_id),
    )


def current_context() -> dict[str, str | None]:
    """Return every context field, using ``None`` for the ones not set.

    Fields are always present: a missing key in a log record is harder to query
    in Loki than an explicit ``null``.

    The trace fields are read from OpenTelemetry rather than stored, so a log
    record names the span that actually produced it. A consumer restoring the
    context of a message from Kafka headers, where no span is being recorded,
    falls back to the values set through :func:`set_trace_context`.
    """
    values = {name: variable.get() for name, variable in _VARIABLES.items()}
    span_context = _current_span_context()
    if span_context is not None:
        values["trace_id"], values["span_id"] = span_context
    return values


@contextmanager
def bind_context(
    *,
    correlation_id: str | None = None,
    trace_id: str | None = None,
    span_id: str | None = None,
    user_id: str | None = None,
) -> Iterator[None]:
    """Set the given fields for the duration of the block and restore them after.

    Arguments left as ``None`` keep whatever value the surrounding context has.
    Used by consumers and background workers, where no HTTP middleware runs.
    """
    values = {
        "correlation_id": correlation_id,
        "trace_id": trace_id,
        "span_id": span_id,
        "user_id": user_id,
    }
    tokens: list[tuple[ContextVar[str | None], Token[str | None]]] = []
    for name, value in values.items():
        if value is None:
            continue
        variable = _VARIABLES[name]
        tokens.append((variable, variable.set(value)))

    try:
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


@contextmanager
def bind_causation(event_id: UUID) -> Iterator[None]:
    """Name the event whose handling is running for the duration of the block.

    The consumer sets it around a handler, and an event written to the outbox
    from there takes it as its ``causation_id`` -- the way ``correlation_id``
    is taken, so a new handler cannot forget it.
    """
    token = _causation_id.set(event_id)
    try:
        yield
    finally:
        _causation_id.reset(token)
