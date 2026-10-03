"""Observability in the cluster, read as the charts render it.

The cluster runs what the compose stack runs: the same images, the same
alerting rules, dashboards and routing, the same data source uids and log
labels. These tests hold the two together, so that a dashboard or an alert
fixed in one place is fixed in both.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy/helm/observability"
SERVICE_CHART = ROOT / "deploy/helm/service"
VALUES = ROOT / "deploy/helm/values"
OBSERVABILITY = ROOT / "deploy/observability"
COMPOSE_OBS = ROOT / "deploy/compose/docker-compose.obs.yml"
COMPOSE_ALLOY = ROOT / "deploy/compose/observability/config.alloy"
KUBE_PROMETHEUS_STACK = CHART / "kube-prometheus-stack.yaml"
SERVICES = ("api-gateway", "auth", "catalog", "booking", "notification")
SERVICE_MONITOR_API = "monitoring.coreos.com/v1/ServiceMonitor"

Manifest = dict[str, Any]


def helm_template(*arguments: str) -> list[Manifest]:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not on PATH; CI installs it")
    result = subprocess.run(  # noqa: S603
        [helm, "template", *arguments], capture_output=True, text=True, check=True
    )
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def render() -> list[Manifest]:
    return helm_template("observability", str(CHART), "--namespace", "observability")


def render_service(service: str, *extra: str) -> list[Manifest]:
    """Render a service as kind installs it; ``extra`` goes last, as an overlay does."""
    files = [VALUES / "common.yaml", VALUES / f"{service}.yaml", VALUES / "local.yaml"]
    arguments = [service, str(SERVICE_CHART), "--namespace", "barber"]
    for values in files:
        arguments += ["-f", str(values)]
    return helm_template(*arguments, *extra)


def named(manifests: list[Manifest], kind: str, name: str) -> Manifest:
    [found] = [m for m in manifests if m["kind"] == kind and m["metadata"]["name"] == name]
    return found


def kube_prometheus_stack() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(KUBE_PROMETHEUS_STACK.read_text())
    return loaded


# --- the files of the compose stack, not copies of them -----------------------


def test_rules_are_the_rule_file_of_the_compose_stack() -> None:
    rule = named(render(), "PrometheusRule", "barber")

    shared = yaml.safe_load((OBSERVABILITY / "alerts/rules.yaml").read_text())

    assert rule["spec"]["groups"] == shared["groups"]


def test_every_dashboard_of_the_repository_is_provisioned() -> None:
    configmaps = [
        m
        for m in render()
        if m["kind"] == "ConfigMap" and "grafana_dashboard" in m["metadata"].get("labels", {})
    ]

    provisioned = {
        name: json.loads(body)
        for configmap in configmaps
        for name, body in configmap["data"].items()
    }
    files = {
        path.name: json.loads(path.read_text())
        for path in (OBSERVABILITY / "dashboards").glob("*.json")
    }

    assert provisioned == files
    assert {c["metadata"]["annotations"]["grafana_folder"] for c in configmaps} == {"Barber"}


def test_alertmanager_routes_by_the_file_of_the_compose_stack() -> None:
    secret = named(render(), "Secret", "alertmanager-barber")

    routing = yaml.safe_load(secret["stringData"]["alertmanager.yaml"])

    assert routing == yaml.safe_load((OBSERVABILITY / "alertmanager.yaml").read_text())
    assert (
        kube_prometheus_stack()["alertmanager"]["alertmanagerSpec"]["configSecret"]
        == "alertmanager-barber"
    )


@pytest.mark.parametrize("component", ["tempo", "loki", "alloy"])
def test_cluster_runs_the_image_of_the_compose_stack(component: str) -> None:
    compose = yaml.safe_load(COMPOSE_OBS.read_text())

    deployment = named(render(), "Deployment", component)
    [container] = deployment["spec"]["template"]["spec"]["containers"]

    assert container["image"] == compose["services"][component]["image"]


@pytest.mark.parametrize("component", ["tempo", "loki"])
def test_cluster_runs_the_configuration_of_the_compose_stack(component: str) -> None:
    configmap = named(render(), "ConfigMap", component)

    configured = yaml.safe_load(configmap["data"][f"{component}.yaml"])

    shared = ROOT / f"deploy/compose/observability/{component}.yaml"
    assert configured == yaml.safe_load(shared.read_text())


# --- Grafana ------------------------------------------------------------------


def datasource_uids_of_dashboards() -> set[str]:
    uids = set()
    for path in (OBSERVABILITY / "dashboards").glob("*.json"):
        uids |= set(re.findall(r'"datasource":\s*\{[^}]*"uid":\s*"([^"$]+)"', path.read_text()))
    return uids


def test_every_data_source_a_dashboard_names_is_provisioned() -> None:
    grafana = kube_prometheus_stack()["grafana"]
    sidecar = grafana["sidecar"]["datasources"]

    provisioned = {sidecar["uid"], sidecar["alertmanager"]["uid"]} | {
        source["uid"] for source in grafana["additionalDataSources"]
    }

    assert datasource_uids_of_dashboards() <= provisioned
    assert provisioned == {"prometheus", "alertmanager", "tempo", "loki"}


def test_traces_and_logs_link_to_each_other_as_in_the_compose_stack() -> None:
    compose = yaml.safe_load(
        (ROOT / "deploy/compose/observability/grafana-datasources.yaml").read_text()
    )
    by_uid = {source["uid"]: source for source in compose["datasources"]}

    cluster = {
        source["uid"]: source
        for source in kube_prometheus_stack()["grafana"]["additionalDataSources"]
    }

    for uid in ("tempo", "loki"):
        assert cluster[uid]["jsonData"] == by_uid[uid]["jsonData"]


def test_prometheus_selects_the_monitors_and_rules_of_other_releases() -> None:
    spec = kube_prometheus_stack()["prometheus"]["prometheusSpec"]

    assert spec["serviceMonitorSelectorNilUsesHelmValues"] is False
    assert spec["ruleSelectorNilUsesHelmValues"] is False
    assert (
        spec["scrapeInterval"]
        == yaml.safe_load((ROOT / "deploy/compose/observability/prometheus.yml").read_text())[
            "global"
        ]["scrape_interval"]
    )


# --- logs ---------------------------------------------------------------------


def alloy_rules(config: str) -> list[str]:
    """The relabeling rules of a config.alloy, whitespace collapsed."""
    return [" ".join(rule.split()) for rule in re.findall(r"rule \{(.*?)\}", config, re.DOTALL)]


def test_logs_are_labelled_as_in_the_compose_stack() -> None:
    compose = COMPOSE_ALLOY.read_text()
    cluster = named(render(), "ConfigMap", "alloy")["data"]["config.alloy"]

    # The processing of a line -- which fields become labels, which metadata.
    def processing(config: str) -> str:
        block = re.search(r'loki\.process "services" \{(.*?)forward_to', config, re.DOTALL)
        assert block is not None
        return " ".join(re.sub(r"//.*", "", block.group(1)).split())

    assert processing(cluster) == processing(compose)
    # The labels the source adds by itself are dropped before processing.
    assert 'regex = "instance|job"' in " ".join(cluster.split())
    assert 'target_label = "service"' in " ".join(alloy_rules(cluster))
    assert 'target_label = "env"' in " ".join(alloy_rules(cluster))


def test_alloy_reads_the_logs_of_the_platform_namespace_only() -> None:
    manifests = render()

    role = named(manifests, "Role", "alloy-logs")

    assert role["metadata"]["namespace"] == "barber"
    assert role["rules"] == [
        {"apiGroups": [""], "resources": ["pods", "pods/log"], "verbs": ["get", "list", "watch"]}
    ]
    assert [m for m in manifests if m["kind"] in {"ClusterRole", "ClusterRoleBinding"}] == []


# --- the services ---------------------------------------------------------------


@pytest.mark.parametrize("service", SERVICES)
def test_service_is_scraped_where_the_operator_is(service: str) -> None:
    manifests = render_service(service, "--api-versions", SERVICE_MONITOR_API)

    monitor = named(manifests, "ServiceMonitor", service)
    selector = named(manifests, "Service", service)["metadata"]["labels"]
    [endpoint] = monitor["spec"]["endpoints"]

    assert monitor["spec"]["selector"]["matchLabels"].items() <= selector.items()
    assert (endpoint["port"], endpoint["path"], endpoint["interval"]) == ("http", "/metrics", "15s")
    assert endpoint["relabelings"] == [{"targetLabel": "env", "replacement": "local"}]


@pytest.mark.parametrize("service", SERVICES)
def test_service_installs_without_the_operator(service: str) -> None:
    manifests = render_service(service)

    assert [m for m in manifests if m["kind"] == "ServiceMonitor"] == []


@pytest.mark.parametrize("service", SERVICES)
def test_overlay_sends_the_traces_to_tempo_of_the_cluster(service: str) -> None:
    manifests = render_service(service, "-f", str(VALUES / "observability.yaml"))

    data = named(manifests, "ConfigMap", service)["data"]
    tempo = named(render(), "Service", "tempo")

    grpc = next(p["port"] for p in tempo["spec"]["ports"] if p["name"] == "otlp-grpc")
    assert data["OTLP_ENABLED"] == "true"
    assert data["OTLP_ENDPOINT"] == f"http://tempo.observability.svc.cluster.local:{grpc}"
