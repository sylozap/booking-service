"""Prometheus metrics: RED for HTTP, helpers for the domain counters.

One rule governs everything here: an identifier never becomes a label. A label
whose value is a booking id, a master id or a concrete URL multiplies the number
of time series by the number of rows in the database, and the first service to
do it takes Prometheus down with it. The path label therefore holds the route
template, ``/bookings/{id}``, which the instrumentation derives from the router.

A new metric is declared together with the answer to "what decision do I make
when I see it change". A metric without that answer is not declared.
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import FastAPI
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, ProcessCollector
from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_fastapi_instrumentator.metrics import Info

__all__ = [
    "REGISTRY",
    "counter",
    "gauge",
    "histogram",
    "instrument_app",
]

# The registry of the process. Prometheus metrics are process-global by nature:
# a scrape has to see every counter, whichever module declared it.
REGISTRY = CollectorRegistry(auto_describe=True)

# Memory, CPU and, above all, process_start_time_seconds: a restart is a change
# of it. That answers "how often does it restart" the same way under compose
# and in the cluster, where kube-state-metrics is not always there.
ProcessCollector(registry=REGISTRY)

METRICS_ENDPOINT = "/metrics"

# Buckets aimed at the SLO of the platform: 500 ms for creating a booking,
# 1 s for an availability query.
DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

_SUFFIXES = ("_total", "_seconds", "_bytes", "_messages", "_lag", "_timestamp", "_ratio")

HTTP_REQUESTS = Counter(
    "http_requests_total",
    "HTTP requests handled by the service",
    labelnames=("method", "path", "status"),
    registry=REGISTRY,
)

# Status stays off the histogram on purpose: a histogram already multiplies its
# labels by the number of buckets, and the error rate is answered by the counter.
HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "Time spent handling an HTTP request",
    labelnames=("method", "path"),
    buckets=DURATION_BUCKETS,
    registry=REGISTRY,
)


def _check_name(name: str) -> None:
    """Reject a metric name that does not say what it measures.

    The convention is ``<subject>_<unit>_total`` for counters and ``_seconds``
    for durations; it is what makes a dashboard readable without a legend.
    """
    if not name.endswith(_SUFFIXES):
        raise ValueError(f"metric {name!r} has to end with one of {', '.join(_SUFFIXES)}")


def counter(name: str, documentation: str, labelnames: Sequence[str] = ()) -> Counter:
    """Declare a domain counter, for example ``bookings_created_total``."""
    _check_name(name)
    return Counter(name, documentation, labelnames=labelnames, registry=REGISTRY)


def histogram(
    name: str,
    documentation: str,
    labelnames: Sequence[str] = (),
    buckets: Sequence[float] = DURATION_BUCKETS,
) -> Histogram:
    """Declare a domain histogram, for example ``availability_query_duration_seconds``."""
    _check_name(name)
    return Histogram(
        name,
        documentation,
        labelnames=labelnames,
        buckets=tuple(buckets),
        registry=REGISTRY,
    )


def gauge(name: str, documentation: str, labelnames: Sequence[str] = ()) -> Gauge:
    """Declare a domain gauge, for example ``outbox_pending_messages``."""
    _check_name(name)
    return Gauge(name, documentation, labelnames=labelnames, registry=REGISTRY)


def _record_request(info: Info) -> None:
    """Turn one handled request into the RED metrics."""
    HTTP_REQUESTS.labels(
        method=info.method,
        path=info.modified_handler,
        status=info.modified_status,
    ).inc()
    HTTP_REQUEST_DURATION.labels(
        method=info.method,
        path=info.modified_handler,
    ).observe(info.modified_duration)


def instrument_app(app: FastAPI) -> None:
    """Add the RED metrics and expose ``/metrics``.

    Called before the application starts serving: adding middleware to a running
    application is not possible.
    """
    instrumentator = Instrumentator(
        registry=REGISTRY,
        should_group_status_codes=False,
        # An unmatched path is reported as "none" rather than as itself: a
        # scanner walking random URLs would otherwise create a series each.
        should_group_untemplated=True,
        excluded_handlers=[METRICS_ENDPOINT, "/health/live", "/health/ready"],
    )
    instrumentator.add(_record_request)
    instrumentator.instrument(app)
    instrumentator.expose(
        app,
        endpoint=METRICS_ENDPOINT,
        include_in_schema=False,
        should_gzip=False,
    )
