"""The kind stand: its cluster, its Ingress controller and the script tying them.

What these files promise each other is checked here, since none of them can
check the others: the node runs the Kubernetes the manifests are validated
against, Traefik sits where the host ports are, and the script installs every
service with the gateway last.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CLUSTER = ROOT / "deploy/kind/cluster.yaml"
TRAEFIK = ROOT / "deploy/kind/traefik.yaml"
KIND_UP = ROOT / "scripts/kind_up.sh"
MAKEFILE = ROOT / "Makefile"
LOCAL = ROOT / "deploy/helm/values/local.yaml"


def load(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(path.read_text())
    return loaded


def makefile_variable(name: str) -> str:
    found = re.search(rf"^{name} := (\S+)$", MAKEFILE.read_text(), re.MULTILINE)
    assert found is not None
    return found.group(1)


def script_array(name: str) -> list[str]:
    found = re.search(rf"^{name}=\((.*?)\)$", KIND_UP.read_text(), re.MULTILINE)
    assert found is not None
    return found.group(1).split()


def test_node_runs_the_kubernetes_the_manifests_are_validated_against() -> None:
    [node] = load(CLUSTER)["nodes"]

    image = node["image"]

    assert image.startswith(f"kindest/node:v{makefile_variable('KUBERNETES_VERSION')}@sha256:")


def test_host_reaches_ports_80_and_443_of_the_node_and_nothing_else_does() -> None:
    [node] = load(CLUSTER)["nodes"]

    mappings = {
        (m["containerPort"], m["hostPort"], m["listenAddress"]) for m in node["extraPortMappings"]
    }

    assert mappings == {(80, 80, "127.0.0.1"), (443, 443, "127.0.0.1")}


def test_traefik_listens_on_the_mapped_ports_of_the_node_that_has_them() -> None:
    [node] = load(CLUSTER)["nodes"]
    traefik = load(TRAEFIK)

    assert traefik["nodeSelector"].items() <= node["labels"].items()
    assert traefik["ports"]["web"]["hostPort"] == 80
    assert traefik["ports"]["websecure"]["hostPort"] == 443


def test_traefik_waits_for_no_load_balancer_kind_does_not_have() -> None:
    assert load(TRAEFIK)["service"]["spec"]["type"] == "ClusterIP"


def test_gateway_ingress_names_the_class_traefik_creates() -> None:
    assert load(TRAEFIK)["ingressClass"]["name"] == load(LOCAL)["ingress"]["className"]


def test_script_installs_every_service_and_the_gateway_last() -> None:
    services = script_array("SERVICES")

    helm_services = re.search(r"^HELM_SERVICES := (.+)$", MAKEFILE.read_text(), re.MULTILINE)

    assert helm_services is not None
    assert sorted(services) == sorted(helm_services.group(1).split())
    # It carries the seed Job, which needs the other four running.
    assert services[-1] == "api-gateway"


def test_etcd_keeps_its_data_in_memory() -> None:
    [node] = load(CLUSTER)["nodes"]

    patches = [yaml.safe_load(patch) for patch in node["kubeadmConfigPatches"]]

    [cluster] = [p for p in patches if p["kind"] == "ClusterConfiguration"]
    # /tmp of a kind node is a tmpfs.
    assert Path(cluster["etcd"]["local"]["dataDir"]).parts[:2] == ("/", "tmp")
