"""Dashboards and alerts against the metrics the code declares.

Grafana and Prometheus accept a query about a metric nobody exports: the panel
shows "No data", the alert stays green forever. A metric renamed in the code
and not in the JSON is exactly that, and nothing else would notice. So every
metric name in a dashboard or a rule has to be declared by some service, or be
one of the few Prometheus makes itself.

The same files are checked for what makes them usable at all: a data source
Grafana provisions, and an alert that says what to do and where to look.
"""

from __future__ import annotations

import importlib
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from barber_common.metrics import REGISTRY

ROOT = Path(__file__).resolve().parents[2]
DASHBOARDS = ROOT / "deploy/observability/dashboards"
RULES = ROOT / "deploy/observability/alerts/rules.yaml"
DATASOURCES = ROOT / "deploy/compose/observability/grafana-datasources.yaml"

# Every module that declares a metric. Importing registers them in REGISTRY.
METRIC_MODULES = (
    "barber_common.metrics",
    "barber_common.auth.jwks",
    "barber_common.cache",
    "barber_common.db.engine",
    "barber_common.kafka.consumer",
    "barber_common.kafka.dlq",
    "barber_common.outbox.relay",
    "barber_auth.workers.cleanup",
    "barber_booking.metrics",
    "barber_notification.metrics",
)

# Series Prometheus writes itself rather than scrapes.
PROMETHEUS_SERIES = frozenset({"up", "ALERTS", "ALERTS_FOR_STATE"})

SEVERITIES = frozenset({"critical", "warning"})
ANNOTATIONS = ("summary", "description", "dashboard")

_STRINGS = re.compile(r'"(?:[^"\\]|\\.)*"')
_MATCHERS = re.compile(r"\{[^}]*\}")
_RANGES = re.compile(r"\[[^\]]*\]")
_GROUPING = re.compile(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)")
_VARIABLES = re.compile(r"\$\w+")
_IDENTIFIER = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_LABEL_VALUES = re.compile(r"label_values\(\s*([a-zA-Z_:][a-zA-Z0-9_:]*)\s*,")
_OPERATORS = frozenset({"and", "or", "unless", "bool", "offset", "inf", "nan"})


def metric_names(expr: str) -> set[str]:
    """The metric names a PromQL expression selects.

    Not a parser: what is not a metric name -- strings, label matchers, ranges,
    groupings, Grafana variables -- is cut out, and of the identifiers left a
    function is the one followed by a parenthesis.
    """
    stripped = _STRINGS.sub("", expr)
    for pattern in (_MATCHERS, _RANGES, _GROUPING, _VARIABLES):
        stripped = pattern.sub(" ", stripped)
    names = set()
    for match in _IDENTIFIER.finditer(stripped):
        rest = stripped[match.end() :].lstrip()
        if rest.startswith("(") or match.group() in _OPERATORS:
            continue
        names.add(match.group())
    return names


def declared_metrics() -> set[str]:
    """The names a scrape of the platform can contain, samples included."""
    for module in METRIC_MODULES:
        importlib.import_module(module)
    names = set(PROMETHEUS_SERIES)
    for family in REGISTRY.collect():
        if family.type == "counter":
            names.add(f"{family.name}_total")
        elif family.type == "histogram":
            names.update(f"{family.name}{suffix}" for suffix in ("_bucket", "_count", "_sum"))
        else:
            names.add(family.name)
    return names


def dashboards() -> list[Path]:
    return sorted(DASHBOARDS.glob("*.json"))


def load_dashboard(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(path.read_text())
    return loaded


def panels_of(dashboard: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for panel in dashboard["panels"]:
        yield panel
        yield from panel.get("panels", [])


def datasource_uids(panel: dict[str, Any]) -> Iterator[str]:
    """The data sources a panel and its queries name."""
    for owner in [panel, *panel.get("targets", [])]:
        source = owner.get("datasource")
        if source:
            yield source["uid"]


def dashboard_queries(dashboard: dict[str, Any]) -> Iterator[tuple[str, set[str]]]:
    """Each query of the dashboard with the metrics it reads."""
    for panel in panels_of(dashboard):
        for target in panel.get("targets", []):
            if "expr" in target:
                yield target["expr"], metric_names(target["expr"])
    for variable in dashboard["templating"]["list"]:
        definition = variable.get("definition", "")
        found = _LABEL_VALUES.search(definition)
        if found:
            yield definition, {found.group(1)}


def alert_rules() -> list[dict[str, Any]]:
    document = yaml.safe_load(RULES.read_text())
    return [rule for group in document["groups"] for rule in group["rules"]]


def provisioned_datasources() -> set[str]:
    document = yaml.safe_load(DATASOURCES.read_text())
    return {source["uid"] for source in document["datasources"]}


def test_every_metric_module_is_found() -> None:
    # Guards the list above: a metric declared in a module missing from it
    # would be reported as unknown, which is the right failure, but a module
    # that moved should fail here, by name.
    for module in METRIC_MODULES:
        importlib.import_module(module)


def test_the_dashboards_are_all_there() -> None:
    uids = [load_dashboard(path)["uid"] for path in dashboards()]

    assert sorted(uids) == ["barber-async", "barber-business", "barber-overview"]


@pytest.mark.parametrize("path", dashboards(), ids=lambda path: path.name)
def test_a_dashboard_reads_only_metrics_that_exist(path: Path) -> None:
    known = declared_metrics()

    unknown = {
        query: sorted(names - known)
        for query, names in dashboard_queries(load_dashboard(path))
        if names - known
    }

    assert unknown == {}


@pytest.mark.parametrize("path", dashboards(), ids=lambda path: path.name)
def test_a_dashboard_uses_only_provisioned_data_sources(path: Path) -> None:
    used = {uid for panel in panels_of(load_dashboard(path)) for uid in datasource_uids(panel)}

    assert used <= provisioned_datasources()


@pytest.mark.parametrize("rule", alert_rules(), ids=lambda rule: rule["alert"])
def test_an_alert_reads_only_metrics_that_exist(rule: dict[str, Any]) -> None:
    assert metric_names(rule["expr"]) - declared_metrics() == set()


@pytest.mark.parametrize("rule", alert_rules(), ids=lambda rule: rule["alert"])
def test_an_alert_says_what_to_do_and_where_to_look(rule: dict[str, Any]) -> None:
    uids = {load_dashboard(path)["uid"] for path in dashboards()}
    annotations = rule.get("annotations", {})

    assert rule["labels"]["severity"] in SEVERITIES
    assert all(annotations.get(name) for name in ANNOTATIONS)
    assert annotations["dashboard"].rsplit("/d/", 1)[-1] in uids


def test_the_alerts_of_the_documented_table_are_all_there() -> None:
    names = {rule["alert"] for rule in alert_rules()}

    assert names == {
        "ServiceDown",
        "HighErrorRate",
        "SlowAvailability",
        "ConsumerLag",
        "OutboxNotDraining",
        "DeadLetters",
        "NotificationDeliveryStalled",
        "ReminderSchedulerSilent",
        "AuthCleanupSilent",
    }


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        (
            'sum(rate(http_requests_total{service=~"$service"}[$__rate_interval]))',
            {"http_requests_total"},
        ),
        (
            "histogram_quantile(0.95, sum by (le) (rate(x_seconds_bucket[5m]))) > 1",
            {"x_seconds_bucket"},
        ),
        (
            "(time() - max by (service) (a_timestamp) > 300) and on (service) (b_seconds > 1)",
            {"a_timestamp", "b_seconds"},
        ),
        ("sum(increase(x_total[$__range])) or vector(0)", {"x_total"}),
        ("up == 0", {"up"}),
    ],
)
def test_metric_names_are_read_out_of_promql(expr: str, expected: set[str]) -> None:
    assert metric_names(expr) == expected
