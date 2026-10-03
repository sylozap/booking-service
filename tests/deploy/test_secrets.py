"""The secret generator against the charts that read what it generates.

``scripts/gen_secrets.sh`` runs for real, against a ``kubectl`` that keeps the
cluster in a JSON file: the script is the code under test, the cluster is the
boundary. What it applied is then held to the rendered charts -- every Secret
and key a pod reads exists -- and to what the services expect inside the
values.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import yaml

from barber_auth.adapters.rsa_signer import RsaTokenSigner
from barber_auth.domain.contacts import normalize_email, normalize_phone
from barber_auth.domain.passwords import check_password_policy
from barber_auth.settings import BootstrapAdminConfig, ServiceClientConfig

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/gen_secrets.sh"
SERVICE_CHART = ROOT / "deploy/helm/service"
INFRA_CHART = ROOT / "deploy/helm/infra"
VALUES = ROOT / "deploy/helm/values"
SERVICES = ("api-gateway", "auth", "catalog", "booking", "notification")
ROLES = ("auth", "catalog", "booking", "notification")

Secrets = dict[tuple[str, str], dict[str, str]]

# A kubectl that understands the two calls the script makes: a server-side
# apply of a manifest on stdin, and a jsonpath read of one key of a Secret.
# Every command line is recorded, so the test can tell whether a value ever
# appeared on one.
FAKE_KUBECTL = textwrap.dedent(
    """\
    #!{python}
    import json, os, sys
    import yaml

    state_path = os.environ["FAKE_KUBE_STATE"]
    state = json.load(open(state_path)) if os.path.exists(state_path) else {{}}
    state.setdefault("argv", []).append(sys.argv[1:])
    state.setdefault("objects", {{}})

    def save():
        json.dump(state, open(state_path, "w"))

    args = sys.argv[1:]
    if args[0] == "apply":
        manifest = yaml.safe_load(sys.stdin)
        metadata = manifest["metadata"]
        key = "/".join([manifest["kind"], metadata.get("namespace", ""), metadata["name"]])
        state["objects"][key] = manifest
        save()
    elif args[:2] == ["get", "secret"]:
        save()
        name = args[2]
        namespace = args[args.index("--namespace") + 1]
        found = state["objects"].get("/".join(["Secret", namespace, name]))
        if found is None:
            sys.stderr.write("NotFound\\n")
            sys.exit(1)
        path = args[args.index("-o") + 1]
        key = path.removeprefix("jsonpath={{.data.").removesuffix("}}").replace("\\\\.", ".")
        sys.stdout.write(found["data"].get(key, ""))
    else:
        sys.stderr.write("fake kubectl does not know: %s\\n" % args)
        sys.exit(2)
    """
)


class FakeCluster:
    """The state the fake kubectl keeps, and a way to run the script on it."""

    def __init__(self, directory: Path) -> None:
        self.state = directory / "cluster.json"
        bin_directory = directory / "bin"
        bin_directory.mkdir()
        kubectl = bin_directory / "kubectl"
        kubectl.write_text(FAKE_KUBECTL.format(python=sys.executable), encoding="utf-8")
        kubectl.chmod(0o755)
        self.path = f"{bin_directory}{os.pathsep}{os.environ['PATH']}"

    def run(self, *arguments: str, env: dict[str, str] | None = None) -> None:
        if shutil.which("uv") is None:
            pytest.skip("uv is not on PATH; the script generates the key with it")
        environment = {**os.environ, "PATH": self.path, "FAKE_KUBE_STATE": str(self.state)}
        environment.pop("TELEGRAM_BOT_TOKEN", None)
        environment.update(env or {})
        subprocess.run(  # noqa: S603
            [str(SCRIPT), *arguments],
            cwd=ROOT,
            env=environment,
            check=True,
            capture_output=True,
        )

    def _loaded(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.state.read_text())
        return loaded

    def secrets(self) -> Secrets:
        """Every Secret applied, keyed by namespace and name, values decoded."""
        return {
            (manifest["metadata"]["namespace"], manifest["metadata"]["name"]): {
                key: base64.b64decode(value).decode() for key, value in manifest["data"].items()
            }
            for manifest in self._loaded()["objects"].values()
            if manifest["kind"] == "Secret"
        }

    def command_lines(self) -> list[str]:
        return [" ".join(argv) for argv in self._loaded()["argv"]]


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> FakeCluster:
    cluster = FakeCluster(tmp_path_factory.mktemp("cluster"))
    cluster.run()
    return cluster


@pytest.fixture
def fresh_cluster(tmp_path: Path) -> FakeCluster:
    return FakeCluster(tmp_path)


def render(chart: Path, release: str, namespace: str, *arguments: str) -> list[dict[str, Any]]:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not on PATH; CI installs it")
    result = subprocess.run(  # noqa: S603
        [helm, "template", release, str(chart), "--namespace", namespace, *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def render_service(service: str) -> list[dict[str, Any]]:
    return render(
        SERVICE_CHART,
        service,
        "barber",
        "-f",
        str(VALUES / "common.yaml"),
        "-f",
        str(VALUES / f"{service}.yaml"),
        "-f",
        str(VALUES / "local.yaml"),
    )


def secrets_read_by(manifests: list[dict[str, Any]]) -> set[tuple[str, str]]:
    """Every (Secret, key) a pod of these manifests reads, as a variable or a file."""
    read = set()
    for manifest in manifests:
        if manifest["kind"] not in {"Deployment", "Job"}:
            continue
        pod = manifest["spec"]["template"]["spec"]
        for container in pod["containers"]:
            for variable in container.get("env", []):
                reference = variable.get("valueFrom", {}).get("secretKeyRef")
                if reference:
                    read.add((reference["name"], reference["key"]))
        for volume in pod.get("volumes", []):
            secret = volume.get("secret")
            if secret:
                read |= {(secret["secretName"], item["key"]) for item in secret["items"]}
    return read


def password_of(dsn: str) -> str:
    password = urlsplit(dsn).password
    assert password is not None
    return password


# --- what the releases read ---------------------------------------------------


@pytest.mark.parametrize("service", SERVICES)
def test_every_secret_a_release_reads_is_generated(generated: FakeCluster, service: str) -> None:
    secrets = generated.secrets()

    read = secrets_read_by(render_service(service))

    missing = {(name, key) for name, key in read if key not in secrets.get(("barber", name), {})}
    assert missing == set()


def test_postgres_gets_every_password_its_init_script_reads(generated: FakeCluster) -> None:
    # The chart's own throwaway Secret names the keys the init script expects.
    [own] = [m for m in render(INFRA_CHART, "infra", "barber-infra") if m["kind"] == "Secret"]
    existing = "postgres.existingSecret=postgres-credentials"
    installed = render(INFRA_CHART, "infra", "barber-infra", "--set", existing)

    generated_keys = generated.secrets()[("barber-infra", "postgres-credentials")].keys()

    assert [m for m in installed if m["kind"] == "Secret"] == []
    assert generated_keys == own["stringData"].keys()


# --- what is inside the values ------------------------------------------------


@pytest.mark.parametrize("role", ROLES)
def test_each_service_connects_as_its_own_role_with_its_password(
    generated: FakeCluster, role: str
) -> None:
    secrets = generated.secrets()
    dsn = urlsplit(secrets[("barber", f"{role}-env")]["DATABASE_DSN"])

    passwords = secrets[("barber-infra", "postgres-credentials")]

    assert dsn.username == role
    assert dsn.path == f"/{role}"
    assert dsn.hostname == "postgres.barber-infra.svc.cluster.local"
    assert dsn.password == passwords[f"{role.upper()}_DB_PASSWORD"]


def test_roles_do_not_share_a_password(generated: FakeCluster) -> None:
    passwords = generated.secrets()[("barber-infra", "postgres-credentials")]

    assert len(set(passwords.values())) == len(passwords)


def test_booking_presents_the_secret_auth_registers_for_it(generated: FakeCluster) -> None:
    secrets = generated.secrets()

    clients = json.loads(secrets[("barber", "auth-env")]["SERVICE_CLIENTS"])
    booking = ServiceClientConfig.model_validate(clients["booking"])

    assert (
        booking.secret.get_secret_value()
        == secrets[("barber", "booking-env")]["SERVICE_CLIENT_SECRET"]
    )
    assert "catalog:read" in booking.scopes


def test_signing_key_is_one_auth_signs_with(generated: FakeCluster) -> None:
    pem = generated.secrets()[("barber", "auth-signing-key")]["private.pem"]

    signer = RsaTokenSigner(pem)

    assert signer.kid


def test_first_administrator_is_one_auth_accepts(generated: FakeCluster) -> None:
    secret = generated.secrets()[("barber", "auth-env")]

    admin = BootstrapAdminConfig(
        email=secret["BOOTSTRAP_ADMIN_EMAIL"],
        phone=secret["BOOTSTRAP_ADMIN_PHONE"],
        password=secret["BOOTSTRAP_ADMIN_PASSWORD"],
    )

    normalize_email(admin.email)
    normalize_phone(admin.phone)
    check_password_policy(admin.password.get_secret_value())


def test_telegram_token_is_empty_without_a_bot(generated: FakeCluster) -> None:
    token = generated.secrets()[("barber", "notification-env")]["TELEGRAM_BOT_TOKEN"]

    assert token == ""


# --- runs after the first -----------------------------------------------------


def generated_values(secrets: Secrets) -> dict[tuple[str, str, str], str]:
    return {
        (namespace, name, key): value
        for (namespace, name), values in secrets.items()
        for key, value in values.items()
    }


def test_second_run_keeps_every_value(fresh_cluster: FakeCluster) -> None:
    fresh_cluster.run()
    first = generated_values(fresh_cluster.secrets())

    fresh_cluster.run()

    assert generated_values(fresh_cluster.secrets()) == first


# Values the script is told rather than makes up: rotation keeps them.
CONFIGURED = {"BOOTSTRAP_ADMIN_EMAIL", "BOOTSTRAP_ADMIN_PHONE", "TELEGRAM_BOT_TOKEN"}


def test_rotate_replaces_every_generated_value(fresh_cluster: FakeCluster) -> None:
    fresh_cluster.run()
    first = generated_values(fresh_cluster.secrets())

    fresh_cluster.run("--rotate")

    second = generated_values(fresh_cluster.secrets())
    unchanged = {
        key for key, value in first.items() if key[2] not in CONFIGURED and second[key] == value
    }
    assert unchanged == set()


def test_telegram_token_comes_from_the_environment_and_stays(fresh_cluster: FakeCluster) -> None:
    fresh_cluster.run(env={"TELEGRAM_BOT_TOKEN": "123:token-of-the-bot"})

    fresh_cluster.run()

    secret = fresh_cluster.secrets()[("barber", "notification-env")]
    assert secret["TELEGRAM_BOT_TOKEN"] == "123:token-of-the-bot"


# --- where the values never go ------------------------------------------------


def test_no_value_appears_on_a_command_line(generated: FakeCluster) -> None:
    values = [v for v in generated_values(generated.secrets()).values() if len(v) >= 8]

    command_lines = generated.command_lines()

    leaked = [value[:8] for value in values if any(value in line for line in command_lines)]
    assert leaked == []


def repository_files() -> set[str]:
    """What git sees in the working tree, ignored files included.

    Bytecode is left out: Python writes it when the key generator is imported,
    and it holds no value.
    """
    listed = subprocess.run(
        ["git", "status", "--porcelain", "--ignored", "--untracked-files=all"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    return {line for line in listed if "__pycache__" not in line and not line.endswith(".pyc")}


def test_nothing_is_written_into_the_repository(fresh_cluster: FakeCluster) -> None:
    before = repository_files()

    fresh_cluster.run()

    assert repository_files() == before


def test_the_seed_job_reads_only_generated_keys(generated: FakeCluster) -> None:
    secrets = generated.secrets()
    script = ROOT / "scripts/seed.py"

    read = secrets_read_by(
        render(
            SERVICE_CHART,
            "api-gateway",
            "barber",
            "-f",
            str(VALUES / "common.yaml"),
            "-f",
            str(VALUES / "api-gateway.yaml"),
            "-f",
            str(VALUES / "local.yaml"),
            "--set",
            "seed.enabled=true",
            "--set-file",
            f"seed.script={script}",
        )
    )

    assert read
    assert {(name, key) for name, key in read if key not in secrets[("barber", name)]} == set()
