"""The service chart, read as the manifests ``helm template`` renders.

The templates are not read: what reaches the cluster is the rendered document,
and a template that looks right can still render into something else. Each
test renders the chart with the values it is about and looks at the result.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy/helm/service"

Manifest = dict[str, Any]
Render = Callable[..., list[Manifest]]

# Enough for the chart to render: an image and the resources it insists on.
MINIMAL: Mapping[str, Any] = {
    "image": {"repository": "ghcr.io/example/booking", "tag": "abc123"},
    "port": 8003,
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


# --- what a release consists of ----------------------------------------------


def test_release_renders_deployment_service_configmap_and_account(render: Render) -> None:
    manifests = render()

    kinds = sorted(manifest["kind"] for manifest in manifests)

    assert kinds == ["ConfigMap", "Deployment", "Service", "ServiceAccount"]
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


def test_container_reads_the_configmap_and_the_existing_secret(render: Render) -> None:
    manifests = render()

    env_from = container_of(only(manifests, "Deployment"))["envFrom"]

    assert env_from == [
        {"configMapRef": {"name": "booking"}},
        {"secretRef": {"name": "booking-env"}},
    ]


def test_service_without_secrets_references_none(render: Render) -> None:
    manifests = render({"secret": {"enabled": False}})

    env_from = container_of(only(manifests, "Deployment"))["envFrom"]

    assert env_from == [{"configMapRef": {"name": "booking"}}]


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
    values["image"] = {"repository": "ghcr.io/example/booking"}

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
