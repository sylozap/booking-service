"""The service chart, read as the manifests ``helm template`` renders.

The templates are not read: what reaches the cluster is the rendered document,
and a template that looks right can still render into something else. Each
test renders the chart with the values it is about and looks at the result.
"""

from __future__ import annotations

import importlib
import inspect
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

from barber_common.config import BaseAppSettings
from barber_common.kafka.consumer import EventConsumer

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy/helm/service"

Manifest = dict[str, Any]
Render = Callable[..., list[Manifest]]

# Enough for the chart to render: an image and the resources it insists on.
MINIMAL: Mapping[str, Any] = {
    "image": {"registry": "ghcr.io/example", "name": "booking", "tag": "abc123"},
    "port": 8003,
    "secret": {"keys": ["DATABASE_DSN"]},
    "resources": {
        "requests": {"cpu": "100m", "memory": "128Mi"},
        "limits": {"cpu": "500m", "memory": "256Mi"},
    },
}


class RenderError(Exception):
    """``helm template`` refused the chart; the message is what helm said."""


def render_chart(chart: Path, release: str, values_files: list[Path]) -> list[Manifest]:
    """Render a chart and return its manifests, hooks included."""
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not on PATH; CI installs it")

    command = [helm, "template", release, str(chart), "--namespace", "barber"]
    for values in values_files:
        command += ["--values", str(values)]
    result = subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603
    if result.returncode != 0:
        raise RenderError(result.stderr.strip())
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Deep merge, the way helm layers one values file over another."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = merge(merged[key], value)
        else:
            merged[key] = value
    return merged


@pytest.fixture
def render(tmp_path: Path) -> Render:
    """Render the chart as ``booking`` with MINIMAL and the given overrides."""

    def _render(overrides: Mapping[str, Any] | None = None, *, base: bool = True) -> list[Manifest]:
        values = merge(MINIMAL, overrides or {}) if base else dict(overrides or {})
        path = tmp_path / "values.yaml"
        path.write_text(yaml.safe_dump(values), encoding="utf-8")
        return render_chart(CHART, "booking", [path])

    return _render


def find(manifests: list[Manifest], kind: str) -> list[Manifest]:
    return [manifest for manifest in manifests if manifest["kind"] == kind]


def only(manifests: list[Manifest], kind: str) -> Manifest:
    found = find(manifests, kind)
    assert len(found) == 1, f"expected one {kind}, got {len(found)}"
    return found[0]


def pod_spec(workload: Manifest) -> Manifest:
    spec: Manifest = workload["spec"]["template"]["spec"]
    return spec


def container_of(workload: Manifest) -> Manifest:
    containers: list[Manifest] = pod_spec(workload)["containers"]
    assert len(containers) == 1
    return containers[0]


def secret_variables(container: Manifest) -> list[Manifest]:
    """The variables a container takes from a Secret."""
    return [
        variable
        for variable in container.get("env", [])
        if "secretKeyRef" in variable.get("valueFrom", {})
    ]


# --- what a release consists of ----------------------------------------------


def test_release_renders_deployment_service_configmap_account_and_budget(render: Render) -> None:
    manifests = render()

    kinds = sorted(manifest["kind"] for manifest in manifests)

    assert kinds == [
        "ConfigMap",
        "Deployment",
        "PodDisruptionBudget",
        "Service",
        "ServiceAccount",
    ]
    assert {manifest["metadata"]["name"] for manifest in manifests} == {"booking"}


def test_chart_creates_no_secret(render: Render) -> None:
    manifests = render()

    assert find(manifests, "Secret") == []


def test_service_sends_its_port_to_the_named_container_port(render: Render) -> None:
    manifests = render()

    service = only(manifests, "Service")
    port = container_of(only(manifests, "Deployment"))["ports"][0]

    assert service["spec"]["ports"][0]["port"] == 8003
    assert service["spec"]["ports"][0]["targetPort"] == "http"
    assert port == {"name": "http", "containerPort": 8003, "protocol": "TCP"}


def test_service_selects_the_pods_of_the_deployment(render: Render) -> None:
    manifests = render()

    selector = only(manifests, "Service")["spec"]["selector"]
    labels = only(manifests, "Deployment")["spec"]["template"]["metadata"]["labels"]

    assert selector.items() <= labels.items()


# --- configuration -----------------------------------------------------------


def test_configmap_carries_env_and_the_service_name(render: Render) -> None:
    manifests = render({"env": {"LOG_LEVEL": "INFO", "CACHE_TTL_SECONDS": 300}})

    data = only(manifests, "ConfigMap")["data"]

    assert data == {"SERVICE_NAME": "booking", "LOG_LEVEL": "INFO", "CACHE_TTL_SECONDS": "300"}


def test_container_reads_the_configmap_and_each_key_of_the_secret(render: Render) -> None:
    manifests = render({"secret": {"keys": ["DATABASE_DSN", "SERVICE_CLIENT_SECRET"]}})

    container = container_of(only(manifests, "Deployment"))

    assert container["envFrom"] == [{"configMapRef": {"name": "booking"}}]
    assert [
        (variable["name"], variable["valueFrom"]["secretKeyRef"])
        for variable in secret_variables(container)
    ] == [
        ("DATABASE_DSN", {"name": "booking-env", "key": "DATABASE_DSN"}),
        ("SERVICE_CLIENT_SECRET", {"name": "booking-env", "key": "SERVICE_CLIENT_SECRET"}),
    ]


def test_service_without_secrets_references_none(render: Render) -> None:
    manifests = render({"secret": {"enabled": False}})

    container = container_of(only(manifests, "Deployment"))

    assert container["envFrom"] == [{"configMapRef": {"name": "booking"}}]
    assert secret_variables(container) == []


def test_render_fails_when_a_secret_is_enabled_without_keys(render: Render) -> None:
    with pytest.raises(RenderError, match="secret.keys is empty"):
        render({"secret": {"keys": []}})


def test_changed_env_changes_the_pod_template(render: Render) -> None:
    before = only(render({"env": {"LOG_LEVEL": "INFO"}}), "Deployment")
    after = only(render({"env": {"LOG_LEVEL": "DEBUG"}}), "Deployment")

    annotations_before = before["spec"]["template"]["metadata"]["annotations"]
    annotations_after = after["spec"]["template"]["metadata"]["annotations"]

    assert annotations_before["checksum/config"] != annotations_after["checksum/config"]


def test_secret_file_is_mounted_read_only_at_its_path(render: Render) -> None:
    manifests = render(
        {
            "secretFiles": [
                {
                    "secretName": "auth-signing-key",
                    "key": "private.pem",
                    "mountPath": "/run/secrets/auth-signing-key.pem",
                }
            ]
        }
    )

    deployment = only(manifests, "Deployment")
    mount = container_of(deployment)["volumeMounts"][1]
    volume = pod_spec(deployment)["volumes"][1]

    assert mount == {
        "name": "secret-file-0",
        "mountPath": "/run/secrets/auth-signing-key.pem",
        "subPath": "private.pem",
        "readOnly": True,
    }
    assert volume["secret"]["secretName"] == "auth-signing-key"


# --- what the chart refuses --------------------------------------------------


@pytest.mark.parametrize("missing", ["cpu", "memory"])
def test_render_fails_without_a_limit(render: Render, missing: str) -> None:
    limits = {"cpu": "500m", "memory": "256Mi"}
    del limits[missing]
    values = merge(MINIMAL, {})
    values["resources"] = {"requests": MINIMAL["resources"]["requests"], "limits": limits}

    with pytest.raises(RenderError, match=r"resources\.limits\.cpu and resources\.limits\.memory"):
        render(values, base=False)


def test_render_fails_without_requests(render: Render) -> None:
    values = merge(MINIMAL, {})
    values["resources"] = {"limits": MINIMAL["resources"]["limits"]}

    with pytest.raises(RenderError, match=r"resources\.requests"):
        render(values, base=False)


def test_render_fails_without_an_image_tag(render: Render) -> None:
    values = merge(MINIMAL, {})
    values["image"] = {"registry": "ghcr.io/example", "name": "booking"}

    with pytest.raises(RenderError, match="image.tag is required"):
        render(values, base=False)


# --- the pod -----------------------------------------------------------------


def test_pod_runs_unprivileged_on_a_read_only_filesystem(render: Render) -> None:
    deployment = only(render(), "Deployment")

    pod = pod_spec(deployment)
    container = container_of(deployment)

    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["automountServiceAccountToken"] is False
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]


def test_rollout_never_drops_below_the_replica_count(render: Render) -> None:
    deployment = only(render({"replicas": 3}), "Deployment")

    assert deployment["spec"]["replicas"] == 3
    assert deployment["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"] == 0


# --- the values of the five services ----------------------------------------
#
# A service is its values file and nothing else. These tests render each one
# the way it is installed and hold it to what the code of the service expects:
# the port its image listens on, the settings it reads, the topics it consumes.

VALUES = ROOT / "deploy/helm/values"
COMMON = VALUES / "common.yaml"
LOCAL = VALUES / "local.yaml"
SERVICES = ("api-gateway", "auth", "catalog", "booking", "notification")
# The services whose load follows the traffic (docs/10-infrastructure.md).
AUTOSCALED = ("api-gateway", "booking")
TAG = "0123abc"

SETTINGS = {
    "api-gateway": "barber_gateway.settings:GatewaySettings",
    "auth": "barber_auth.settings:AuthSettings",
    "catalog": "barber_catalog.settings:CatalogSettings",
    "booking": "barber_booking.settings:BookingSettings",
    "notification": "barber_notification.settings:NotificationSettings",
}

# The topics each service subscribes to, as its consumers declare them.
CONSUMED_TOPICS = {
    "api-gateway": (),
    "auth": (),
    "catalog": (),
    "booking": (
        "barber_booking.consumers.catalog_events:CATALOG_TOPICS",
        "barber_booking.consumers.master_lifecycle:MASTER_LIFECYCLE_TOPICS",
    ),
    "notification": (
        "barber_notification.consumers.user_events:USER_EVENTS_TOPICS",
        "barber_notification.consumers.booking_events:BOOKING_EVENTS_TOPICS",
    ),
}


def _attribute(reference: str) -> object:
    module, _, attribute = reference.partition(":")
    return getattr(importlib.import_module(module), attribute)


def settings_of(service: str) -> type[BaseAppSettings]:
    settings = _attribute(SETTINGS[service])
    assert isinstance(settings, type) and issubclass(settings, BaseAppSettings)
    return settings


def topics_of(reference: str) -> tuple[str, ...]:
    topics = _attribute(reference)
    assert isinstance(topics, tuple)
    return topics


@pytest.fixture
def render_service(tmp_path: Path) -> Callable[..., list[Manifest]]:
    """Render a service as it is installed: common, its own file, maybe local."""

    def _render(service: str, *, local: bool = False) -> list[Manifest]:
        tag = tmp_path / "tag.yaml"
        tag.write_text(yaml.safe_dump({"image": {"tag": TAG}}), encoding="utf-8")
        files = [COMMON, VALUES / f"{service}.yaml", tag]
        if local:
            files.append(LOCAL)
        return render_chart(CHART, service, files)

    return _render


def replica_range(manifests: list[Manifest]) -> tuple[int, int]:
    """The fewest and the most pods the release may run.

    The autoscaler decides when there is one, the Deployment otherwise.
    """
    autoscalers = find(manifests, "HorizontalPodAutoscaler")
    if autoscalers:
        spec = only(manifests, "HorizontalPodAutoscaler")["spec"]
        return spec["minReplicas"], spec["maxReplicas"]
    replicas: int = only(manifests, "Deployment")["spec"]["replicas"]
    return replicas, replicas


def values_of(service: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((VALUES / f"{service}.yaml").read_text())
    return loaded


@pytest.mark.parametrize("service", SERVICES)
def test_service_renders_from_its_values_alone(
    render_service: Callable[..., list[Manifest]], service: str
) -> None:
    deployment = only(render_service(service), "Deployment")

    container = container_of(deployment)

    assert deployment["metadata"]["name"] == service
    assert container["image"] == f"ghcr.io/sylozap/booking-service/{service}:{TAG}"
    assert replica_range(render_service(service)) == ((2, 5) if service in AUTOSCALED else (2, 2))
    assert container["resources"]["limits"].keys() >= {"cpu", "memory"}


@pytest.mark.parametrize("service", SERVICES)
def test_port_is_the_one_the_image_listens_on(
    render_service: Callable[..., list[Manifest]], service: str
) -> None:
    dockerfile = (ROOT / f"services/{service}/Dockerfile").read_text()
    exposed = re.search(r"^EXPOSE (\d+)$", dockerfile, re.MULTILINE)
    assert exposed is not None

    port = container_of(only(render_service(service), "Deployment"))["ports"][0]

    assert port["containerPort"] == int(exposed.group(1))


@pytest.mark.parametrize("service", SERVICES)
def test_every_variable_is_a_setting_of_the_service(
    render_service: Callable[..., list[Manifest]], service: str
) -> None:
    manifests = render_service(service)
    settings = settings_of(service)
    fields = {name.upper() for name in settings.model_fields}

    configured = set(only(manifests, "ConfigMap")["data"])
    container = container_of(only(manifests, "Deployment"))
    secret = {variable["name"] for variable in secret_variables(container)}

    assert configured | secret <= fields


@pytest.mark.parametrize("service", SERVICES)
def test_every_required_setting_is_provided(
    render_service: Callable[..., list[Manifest]], service: str
) -> None:
    manifests = render_service(service)
    settings = settings_of(service)
    required = {
        name.upper() for name, field in settings.model_fields.items() if field.is_required()
    }

    configured = set(only(manifests, "ConfigMap")["data"])
    container = container_of(only(manifests, "Deployment"))
    secret = {variable["name"] for variable in secret_variables(container)}

    assert required <= configured | secret


@pytest.mark.parametrize("service", SERVICES)
def test_consumed_topics_are_the_ones_the_consumers_subscribe_to(service: str) -> None:
    subscribed = {topic for reference in CONSUMED_TOPICS[service] for topic in topics_of(reference)}

    documented = set(values_of(service).get("consumes", []))

    assert documented == subscribed


@pytest.mark.parametrize("service", SERVICES)
def test_local_runs_one_replica_of_the_image_built_for_compose(
    render_service: Callable[..., list[Manifest]], service: str
) -> None:
    deployment = only(render_service(service, local=True), "Deployment")

    container = container_of(deployment)

    assert replica_range(render_service(service, local=True)) == (
        (1, 3) if service in AUTOSCALED else (1, 1)
    )
    assert container["image"] == f"barber/{service}:local"
    assert container["imagePullPolicy"] == "Never"
    assert container["resources"]["limits"] == {"cpu": "500m", "memory": "384Mi"}


# --- the migration Job --------------------------------------------------------

SERVICES_WITH_SCHEMA = ("auth", "catalog", "booking", "notification")


def job_of(manifests: list[Manifest]) -> Manifest:
    return only(manifests, "Job")


@pytest.mark.parametrize("service", SERVICES)
def test_only_a_service_with_a_schema_has_a_migration_job(
    render_service: Callable[..., list[Manifest]], service: str
) -> None:
    jobs = find(render_service(service), "Job")

    has_schema = (ROOT / f"services/{service}/alembic.ini").exists()

    assert has_schema == (service in SERVICES_WITH_SCHEMA)
    assert len(jobs) == (1 if has_schema else 0)


@pytest.mark.parametrize("service", SERVICES_WITH_SCHEMA)
def test_migration_runs_before_the_release_and_survives_a_failure(
    render_service: Callable[..., list[Manifest]], service: str
) -> None:
    job = job_of(render_service(service))

    annotations = job["metadata"]["annotations"]

    assert annotations["helm.sh/hook"] == "pre-install,pre-upgrade"
    assert annotations["helm.sh/hook-weight"] == "-5"
    assert annotations["helm.sh/hook-delete-policy"] == "before-hook-creation"
    assert pod_spec(job)["restartPolicy"] == "Never"
    assert job["spec"]["activeDeadlineSeconds"] > 0


@pytest.mark.parametrize("service", SERVICES_WITH_SCHEMA)
def test_migration_runs_alembic_of_the_service_in_the_same_image(
    render_service: Callable[..., list[Manifest]], service: str
) -> None:
    manifests = render_service(service)

    migrate = container_of(job_of(manifests))
    application = container_of(only(manifests, "Deployment"))

    assert migrate["command"] == ["alembic", "upgrade", "head"]
    assert migrate["workingDir"] == f"/app/services/{service}"
    assert migrate["image"] == application["image"]
    assert migrate["resources"]["limits"].keys() >= {"cpu", "memory"}


@pytest.mark.parametrize("service", SERVICES_WITH_SCHEMA)
def test_migration_leans_on_nothing_the_release_creates(
    render_service: Callable[..., list[Manifest]], service: str
) -> None:
    job = job_of(render_service(service))

    pod = pod_spec(job)
    migrate = container_of(job)

    assert "serviceAccountName" not in pod
    assert "envFrom" not in migrate
    assert migrate["env"] == [
        {
            "name": "DATABASE_DSN",
            "valueFrom": {"secretKeyRef": {"name": f"{service}-env", "key": "DATABASE_DSN"}},
        }
    ]


@pytest.mark.parametrize("service", SERVICES_WITH_SCHEMA)
def test_service_never_routes_to_the_migration_pod(
    render_service: Callable[..., list[Manifest]], service: str
) -> None:
    manifests = render_service(service)

    selector = only(manifests, "Service")["spec"]["selector"]
    labels = job_of(manifests)["spec"]["template"]["metadata"]["labels"]

    assert not selector.items() <= labels.items()


def test_render_fails_when_migrations_have_no_secret(render: Render) -> None:
    with pytest.raises(RenderError, match="migrations need DATABASE_DSN"):
        render({"migrations": {"enabled": True}, "secret": {"enabled": False}})


# --- probes and the shutdown --------------------------------------------------


@pytest.mark.parametrize("service", SERVICES)
def test_liveness_looks_at_the_process_and_readiness_at_the_dependencies(
    render_service: Callable[..., list[Manifest]], service: str
) -> None:
    container = container_of(only(render_service(service), "Deployment"))

    assert container["startupProbe"]["httpGet"] == {"path": "/health/live", "port": "http"}
    assert container["livenessProbe"]["httpGet"] == {"path": "/health/live", "port": "http"}
    assert container["readinessProbe"]["httpGet"] == {"path": "/health/ready", "port": "http"}


def test_readiness_waits_longer_than_the_checks_behind_it(render: Render) -> None:
    container = container_of(only(render(), "Deployment"))

    # The deadline of all readiness checks together, HEALTH_CHECK_TIMEOUT_SECONDS.
    checks_deadline = BaseAppSettings.model_fields["health_check_timeout_seconds"].default

    assert container["readinessProbe"]["timeoutSeconds"] > checks_deadline


@pytest.mark.parametrize("service", SERVICES)
def test_grace_period_is_the_sum_of_the_shutdown_budget(
    render_service: Callable[..., list[Manifest]], service: str
) -> None:
    deployment = only(render_service(service), "Deployment")

    pod = pod_spec(deployment)
    container = container_of(deployment)
    timeout = {variable["name"]: variable.get("value") for variable in container["env"]}

    assert container["lifecycle"]["preStop"] == {"sleep": {"seconds": 5}}
    assert timeout["UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN"] == "15"
    assert pod["terminationGracePeriodSeconds"] == 5 + 15 + 30 + 5


def test_a_longer_phase_lengthens_the_grace_period(render: Render) -> None:
    deployment = only(render({"shutdown": {"backgroundSeconds": 60}}), "Deployment")

    assert pod_spec(deployment)["terminationGracePeriodSeconds"] == 5 + 15 + 60 + 5


def test_requests_are_given_longer_than_the_gateway_waits_for_an_answer() -> None:
    gateway = load_settings_default("api-gateway", "proxy_read_timeout_seconds")
    chart = yaml.safe_load((CHART / "values.yaml").read_text())

    assert chart["shutdown"]["requestsSeconds"] > gateway


def test_background_phase_fits_the_time_one_message_may_take() -> None:
    message_timeout = inspect.signature(EventConsumer).parameters["message_timeout_seconds"].default
    chart = yaml.safe_load((CHART / "values.yaml").read_text())

    assert chart["shutdown"]["backgroundSeconds"] >= message_timeout


def load_settings_default(service: str, field: str) -> float:
    default = settings_of(service).model_fields[field].default
    assert isinstance(default, int | float)
    return float(default)


# --- autoscaling and disruption budgets ---------------------------------------


@pytest.mark.parametrize("service", SERVICES)
def test_only_the_gateway_and_booking_are_autoscaled(
    render_service: Callable[..., list[Manifest]], service: str
) -> None:
    autoscalers = find(render_service(service), "HorizontalPodAutoscaler")

    assert len(autoscalers) == (1 if service in AUTOSCALED else 0)


@pytest.mark.parametrize("service", AUTOSCALED)
def test_autoscaler_scales_the_deployment_by_cpu(
    render_service: Callable[..., list[Manifest]], service: str
) -> None:
    spec = only(render_service(service), "HorizontalPodAutoscaler")["spec"]
    [metric] = spec["metrics"]

    assert spec["scaleTargetRef"] == {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "name": service,
    }
    assert metric["resource"]["name"] == "cpu"
    assert metric["resource"]["target"] == {"type": "Utilization", "averageUtilization": 70}


@pytest.mark.parametrize("service", AUTOSCALED)
def test_autoscaled_deployment_leaves_the_replica_count_to_the_autoscaler(
    render_service: Callable[..., list[Manifest]], service: str
) -> None:
    deployment = only(render_service(service), "Deployment")

    assert "replicas" not in deployment["spec"]


def test_render_fails_when_the_autoscaler_minimum_is_above_its_maximum(render: Render) -> None:
    with pytest.raises(RenderError, match="minReplicas is above"):
        render({"hpa": {"enabled": True, "minReplicas": 4, "maxReplicas": 2}})


@pytest.mark.parametrize("service", SERVICES)
def test_every_service_has_a_budget_that_selects_its_pods(
    render_service: Callable[..., list[Manifest]], service: str
) -> None:
    manifests = render_service(service, local=True)

    budget = only(manifests, "PodDisruptionBudget")["spec"]
    labels = only(manifests, "Deployment")["spec"]["template"]["metadata"]["labels"]

    # maxUnavailable, so that a single replica in kind still lets a node drain.
    assert budget["maxUnavailable"] == 1
    assert "minAvailable" not in budget
    assert budget["selector"]["matchLabels"].items() <= labels.items()


# --- the way in ------------------------------------------------------------------


@pytest.mark.parametrize("service", SERVICES)
def test_only_the_gateway_is_reachable_from_outside(
    render_service: Callable[..., list[Manifest]], service: str
) -> None:
    ingresses = find(render_service(service, local=True), "Ingress")

    assert len(ingresses) == (1 if service == "api-gateway" else 0)


def test_gateway_ingress_sends_every_path_of_the_host_to_the_gateway(
    render_service: Callable[..., list[Manifest]],
) -> None:
    ingress = only(render_service("api-gateway", local=True), "Ingress")

    [rule] = ingress["spec"]["rules"]
    [path] = rule["http"]["paths"]

    assert ingress["spec"]["ingressClassName"] == "traefik"
    assert rule["host"] == "barber.local"
    assert (path["path"], path["pathType"]) == ("/", "Prefix")
    assert path["backend"]["service"] == {"name": "api-gateway", "port": {"name": "http"}}


def test_gateway_behind_the_ingress_trusts_exactly_one_proxy(
    render_service: Callable[..., list[Manifest]],
) -> None:
    data = only(render_service("api-gateway"), "ConfigMap")["data"]

    assert data["TRUSTED_PROXY_HOPS"] == "1"


def test_render_fails_when_the_ingress_has_no_host(render: Render) -> None:
    with pytest.raises(RenderError, match="ingress.host is required"):
        render({"ingress": {"enabled": True}})
