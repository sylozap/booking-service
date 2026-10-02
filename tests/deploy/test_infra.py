"""The infrastructure of the cluster: its chart, its init script, its topics.

The init script is run for real, by the entrypoint of the same PostgreSQL
image the cluster and compose use, and each role is held to its own database.
The chart is read as ``helm template`` renders it.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import yaml
from testcontainers.community.postgres import PostgresContainer

import barber_common.events as events
from barber_common.kafka.dlq import DLQ_TOPIC_SUFFIX
from barber_common.testing import docker_is_available
from barber_common.testing.fixtures import POSTGRES_IMAGE

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy/helm/infra"
INIT_SCRIPT = CHART / "files/init-databases.sh"
CREATE_TOPICS = ROOT / "scripts/create_topics.sh"

SERVICES = ("auth", "catalog", "booking", "notification")

Manifest = dict[str, Any]


def render() -> list[Manifest]:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not on PATH; CI installs it")
    result = subprocess.run(  # noqa: S603
        [helm, "template", "infra", str(CHART), "--namespace", "barber-infra"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def workload(manifests: list[Manifest], name: str) -> Manifest:
    [found] = [
        manifest
        for manifest in manifests
        if manifest["kind"] in {"Deployment", "StatefulSet"}
        and manifest["metadata"]["name"] == name
    ]
    return found


def env_of(manifest: Manifest) -> dict[str, str]:
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    return {variable["name"]: variable.get("value", "") for variable in container.get("env", [])}


# --- topics ------------------------------------------------------------------


def test_create_topics_knows_every_topic_of_the_platform() -> None:
    declared = {getattr(events, name) for name in events.__all__ if name.endswith("_TOPIC")}
    script = CREATE_TOPICS.read_text()

    listed = re.search(r"^TOPICS=\(\n(.*?)\n\)", script, re.MULTILINE | re.DOTALL)
    assert listed is not None
    created = set(listed.group(1).split())

    assert created == declared


def test_create_topics_names_dead_letter_topics_the_way_the_consumer_does() -> None:
    script = CREATE_TOPICS.read_text()

    assert f"DLQ_SUFFIX={DLQ_TOPIC_SUFFIX}\n" in script


# --- the chart ---------------------------------------------------------------


def test_kafka_creates_no_topic_by_itself_and_defaults_to_three_partitions() -> None:
    kafka = env_of(workload(render(), "kafka"))

    assert kafka["KAFKA_AUTO_CREATE_TOPICS_ENABLE"] == "false"
    assert kafka["KAFKA_NUM_PARTITIONS"] == "3"


def test_kafka_advertises_the_address_the_services_connect_to() -> None:
    kafka = env_of(workload(render(), "kafka"))
    common = yaml.safe_load((ROOT / "deploy/helm/values/common.yaml").read_text())

    assert kafka["KAFKA_ADVERTISED_LISTENERS"] == (
        f"PLAINTEXT://{common['env']['KAFKA_BOOTSTRAP_SERVERS']}"
    )


def test_kafka_does_not_see_service_links() -> None:
    kafka = workload(render(), "kafka")

    assert kafka["spec"]["template"]["spec"]["enableServiceLinks"] is False


def test_postgres_runs_the_init_script_of_the_repository() -> None:
    manifests = render()

    [init] = [
        m
        for m in manifests
        if m["kind"] == "ConfigMap" and m["metadata"]["name"] == "postgres-init"
    ]

    assert init["data"]["init-databases.sh"] == INIT_SCRIPT.read_text().rstrip("\n")


def test_postgres_accepts_the_connections_of_two_replicas_of_each_service() -> None:
    postgres = workload(render(), "postgres")

    args = postgres["spec"]["template"]["spec"]["containers"][0]["args"]

    assert args == ["-c", "max_connections=200"]


@pytest.mark.parametrize("name", ["postgres", "redis", "kafka"])
def test_every_infrastructure_pod_has_limits(name: str) -> None:
    container = workload(render(), name)["spec"]["template"]["spec"]["containers"][0]

    assert container["resources"]["limits"].keys() >= {"cpu", "memory"}


def test_a_named_secret_replaces_the_local_passwords() -> None:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not on PATH; CI installs it")
    result = subprocess.run(  # noqa: S603
        [helm, "template", "infra", str(CHART), "--set", "postgres.existingSecret=generated"],
        capture_output=True,
        text=True,
        check=True,
    )
    manifests = [document for document in yaml.safe_load_all(result.stdout) if document]

    postgres = workload(manifests, "postgres")
    env_from = postgres["spec"]["template"]["spec"]["containers"][0]["envFrom"]

    assert [m for m in manifests if m["kind"] == "Secret"] == []
    assert env_from == [{"secretRef": {"name": "generated"}}]


# --- the init script, run by the postgres image ------------------------------


@pytest.fixture(scope="module")
def initialised_postgres() -> Iterator[PostgresContainer]:
    """PostgreSQL that ran init-databases.sh at its first start, as in the cluster."""
    if not docker_is_available():
        pytest.skip("Docker is not available, integration tests need testcontainers")

    container = PostgresContainer(POSTGRES_IMAGE, driver="asyncpg").with_volume_mapping(
        str(INIT_SCRIPT), "/docker-entrypoint-initdb.d/init-databases.sh", "ro"
    )
    for service in SERVICES:
        container = container.with_env(f"{service.upper()}_DB_PASSWORD", f"{service}-password")
    with container:
        yield container


async def current_owner(container: PostgresContainer, *, role: str, database: str) -> str:
    """Connect as a role with its own password, and ask who owns the database."""
    connection = await asyncpg.connect(
        host=container.get_container_host_ip(),
        port=int(container.get_exposed_port(5432)),
        user=role,
        password=f"{role}-password",
        database=database,
    )
    try:
        owner = await connection.fetchval(
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()"
        )
    finally:
        await connection.close()
    return str(owner)


@pytest.mark.integration
@pytest.mark.parametrize("service", SERVICES)
async def test_role_reaches_its_own_database(
    initialised_postgres: PostgresContainer, service: str
) -> None:
    owner = await current_owner(initialised_postgres, role=service, database=service)

    assert owner == service


@pytest.mark.integration
@pytest.mark.parametrize(
    ("role", "database"),
    [(role, database) for role in SERVICES for database in SERVICES if role != database],
)
async def test_role_is_refused_by_every_other_database(
    initialised_postgres: PostgresContainer, role: str, database: str
) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match="permission denied for database"):
        await current_owner(initialised_postgres, role=role, database=database)
