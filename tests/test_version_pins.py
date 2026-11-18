import re
import shutil
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import pytest
import yaml

from tests.support.workflows import WORKFLOWS

ROOT = WORKFLOWS.parents[1]
VERSION = r"(\d+\.\d+\.\d+)"
IMAGE = re.compile(r"([a-z0-9][a-z0-9./_-]*):([A-Za-z0-9][A-Za-z0-9._-]*)@sha256:([0-9a-f]{64})")
ACTION = re.compile(r"uses:\s*([\w.-]+/[\w./-]+)@(\S+)(?:[ \t]*#[ \t]*(\S+))?")
IMAGE_SOURCES = (
    "Dockerfile",
    "web/Dockerfile",
    "deploy/gateway/Dockerfile",
    "deploy/postgres/Dockerfile",
    "deploy/*.yaml",
    "deploy/*.sh",
    ".github/workflows/*.yml",
    "scripts/*.py",
    "README.md",
)


@dataclass(frozen=True)
class Text:
    path: str
    pattern: str

    def values(self) -> set[str]:
        text = (ROOT / self.path).read_text(encoding="utf-8")
        found = {
            ".".join(match) if isinstance(match, tuple) else match
            for match in re.findall(self.pattern, text, flags=re.MULTILINE)
        }
        assert found, f"{self.path} no longer pins {self.pattern}"
        return found


@dataclass(frozen=True)
class ActionInput:
    path: str
    action: str
    key: str

    def values(self) -> set[str]:
        jobs = yaml.safe_load((ROOT / self.path).read_text(encoding="utf-8"))["jobs"]
        found = {
            str(step["with"][self.key])
            for job in jobs.values()
            for step in job.get("steps", [])
            if str(step.get("uses", "")).startswith(f"{self.action}@")
        }
        assert found, f"{self.path} no longer sets {self.action} {self.key}"
        return found


def action_inputs(action: str, key: str) -> list[ActionInput]:
    return [
        ActionInput(path.relative_to(ROOT).as_posix(), action, key)
        for path in sorted(WORKFLOWS.glob("*.yml"))
        if f"uses: {action}@" in path.read_text(encoding="utf-8")
    ]


PINS: dict[str, list[Text | ActionInput]] = {
    "Python": [
        Text(".python-version", rf"^{VERSION}$"),
        Text("pyproject.toml", rf'requires-python = "=={VERSION}"'),
        Text("Dockerfile", rf"FROM python:{VERSION}-"),
        Text(".github/workflows/ci.yml", rf"python:{VERSION}-slim"),
        Text(".github/workflows/ci.yml", rf"--python {VERSION}"),
        Text(".github/workflows/ci.yml", rf"'uv', 'python', '(?:install|find)', '{VERSION}'"),
        Text(".github/workflows/release.yml", rf"--python {VERSION}"),
        Text("deploy/scan-images.sh", rf"python:{VERSION}-slim"),
        Text("deploy/smoke.sh", rf"--python {VERSION}"),
        Text("README.md", rf"CPython \*\*{VERSION}\*\*"),
        Text("README.md", rf"--python {VERSION}"),
        Text("AGENTS.md", rf"CPython {VERSION}"),
        Text("docs/ARCHITECTURE.md", rf"CPython \*\*{VERSION}\*\*"),
        Text("docs/ARCHITECTURE.md", rf"^Python {VERSION} is fixed"),
        Text("docs/ARCHITECTURE.md", rf"\[Python {VERSION}\]"),
        Text("docs/DEPLOYMENT.md", rf"Python {VERSION} and uv"),
        Text("docs/STACK.md", rf"CPython {VERSION}"),
    ],
    "Python feature release": [
        Text("pyproject.toml", r'^target-version = "py(\d)(\d+)"'),
        Text("pyproject.toml", r'^python_version = "(\d+\.\d+)"'),
        Text("sonar-project.properties", r"^sonar\.python\.version=(\d+\.\d+)$"),
        Text("Dockerfile", r"/usr/local/bin/python(\d+\.\d+) "),
    ],
    "uv": [
        *action_inputs("astral-sh/setup-uv", "version"),
        Text("Dockerfile", rf"ghcr\.io/astral-sh/uv:{VERSION}@"),
        Text("docs/STACK.md", rf"; uv {VERSION}"),
    ],
    "Node.js": [
        *action_inputs("actions/setup-node", "node-version"),
        Text("web/Dockerfile", rf"FROM node:{VERSION}-"),
        Text("docs/STACK.md", rf"Node\.js {VERSION} \(image and CI"),
    ],
    "Node.js local floor": [
        Text("web/package.json", rf'"node": ">={VERSION}"'),
        Text("scripts/dev.py", rf"Node\.js {VERSION}"),
        Text("scripts/dev.py", r"< \((\d+), (\d+), (\d+)\)"),
        Text("README.md", rf"Node\.js \*\*{VERSION}\+\*\*"),
        Text("docs/STACK.md", rf"; {VERSION} is the local floor"),
    ],
    "PostgreSQL in production": [
        Text("deploy/postgres/Dockerfile", r"FROM postgres:(\d+\.\d+)-"),
        Text("docs/DEPLOYMENT.md", r"PostgreSQL (\d+\.\d+) on Alpine"),
        Text("docs/STACK.md", r"PostgreSQL (\d+\.\d+) \(production image\)"),
    ],
    "PostgreSQL in development and CI": [
        Text("scripts/start_postgres.py", r'"postgres:(\d+\.\d+)@'),
        Text("docs/ARCHITECTURE.md", r"PostgreSQL (\d+\.\d+) is the verified"),
        Text("docs/STACK.md", r"and (\d+\.\d+) \(development, CI tests\)"),
    ],
    "RabbitMQ": [
        Text("deploy/compose.yaml", rf"rabbitmq:{VERSION}@"),
        Text(".github/workflows/ci.yml", rf"rabbitmq:{VERSION}@"),
        Text("README.md", rf"rabbitmq:{VERSION}@"),
        Text("scripts/start_rabbitmq.py", rf"rabbitmq-server-windows-{VERSION}\.zip"),
        Text("scripts/start_rabbitmq.py", rf"/download/v{VERSION}/"),
        Text("scripts/start_rabbitmq.py", rf"rabbitmq_server-{VERSION}"),
        Text("docs/STACK.md", rf"RabbitMQ {VERSION}"),
    ],
    "Traefik": [
        Text("deploy/gateway/go.mod", rf"github\.com/traefik/traefik/v3 v{VERSION}"),
        Text("docs/STACK.md", rf"Traefik {VERSION}"),
    ],
    "Go": [
        Text("deploy/gateway/Dockerfile", rf"FROM golang:{VERSION}-"),
        Text("deploy/gateway/go.mod", rf"^go {VERSION}$"),
        Text("docs/STACK.md", rf"Go {VERSION}"),
    ],
    "Terraform": [
        Text(".github/workflows/ci.yml", rf"terraform_version: {VERSION}"),
        Text("infra/terraform/versions.tf", rf'required_version = "= {VERSION}"'),
        Text("docs/STACK.md", rf"Terraform {VERSION}"),
    ],
    "Trivy": [
        Text("deploy/scan-images.sh", rf"aquasec/trivy:{VERSION}@"),
        Text("docs/STACK.md", rf"Trivy {VERSION}"),
    ],
}


def pinned(name: str) -> dict[str, set[str]]:
    found: dict[str, set[str]] = defaultdict(set)
    for location in PINS[name]:
        found[location.path] |= location.values()
    return dict(found)


def single(name: str) -> str:
    (version,) = set[str]().union(*pinned(name).values())
    return version


def release(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


@pytest.mark.parametrize("name", sorted(PINS))
def test_every_location_of_a_pinned_version_names_the_same_version(name: str) -> None:
    found = pinned(name)

    assert len(set[str]().union(*found.values())) == 1, found


def test_the_python_feature_release_follows_the_pinned_interpreter() -> None:
    assert single("Python").rsplit(".", 1)[0] == single("Python feature release")


def test_the_local_node_floor_admits_the_pinned_node_of_the_same_major() -> None:
    floor, pinned_node = release(single("Node.js local floor")), release(single("Node.js"))

    assert floor <= pinned_node
    assert floor[0] == pinned_node[0]


def test_development_postgresql_runs_the_major_version_production_runs() -> None:
    major = single("PostgreSQL in production").split(".")[0]
    start_postgres = (ROOT / "scripts/start_postgres.py").read_text(encoding="utf-8")

    assert single("PostgreSQL in development and CI").split(".")[0] == major
    assert f'"/var/lib/postgresql/{major}/docker"' in start_postgres


def test_the_build_backend_range_admits_the_pinned_uv() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'"uv_build>=(\d+\.\d+\.\d+),<(\d+\.\d+)"', pyproject)
    assert match is not None
    uv = release(single("uv"))

    assert release(match[1]) <= uv
    assert uv[:2] < release(match[2])


def test_every_image_tag_resolves_to_one_digest() -> None:
    digests: dict[tuple[str, str], set[str]] = defaultdict(set)
    for pattern in IMAGE_SOURCES:
        for path in sorted(ROOT.glob(pattern)):
            for name, tag, digest in IMAGE.findall(path.read_text(encoding="utf-8")):
                digests[name, tag].add(digest)
    names = {name for name, _ in digests}

    assert {"python", "node", "rabbitmq", "postgres", "golang", "aquasec/trivy"} <= names
    assert {image: found for image, found in digests.items() if len(found) != 1} == {}


def test_every_action_is_pinned_to_one_commit_and_release() -> None:
    actions: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for action, commit, version in ACTION.findall(path.read_text(encoding="utf-8")):
            actions[action].add((commit, version))

    assert {"actions/checkout", "astral-sh/setup-uv"} <= set(actions)
    assert {
        action: pins
        for action, pins in actions.items()
        if len(pins) != 1
        or not all(re.fullmatch(r"[0-9a-f]{40}", commit) and version for commit, version in pins)
    } == {}


def tracked_files() -> list[PurePosixPath]:
    git = shutil.which("git")
    assert git is not None
    listed = subprocess.run(  # noqa: S603 -- Resolved git executable with fixed arguments.
        [git, "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True
    ).stdout.decode()
    return [PurePosixPath(path) for path in listed.split("\0") if path]


def ecosystem(path: PurePosixPath) -> str | None:
    if path.parent.as_posix() == ".github/workflows":
        return "github-actions"
    if path.name == "go.mod":
        requires = re.search(r"^require\b", (ROOT / path).read_text(encoding="utf-8"), re.M)
        return "gomod" if requires else None
    if path.suffix == ".tf":
        return "terraform"
    manifests = {
        "Dockerfile": "docker",
        "compose.yaml": "docker-compose",
        "package.json": "npm",
        "uv.lock": "uv",
    }
    return manifests.get(path.name)


def used_ecosystems() -> set[tuple[str, str]]:
    used: set[tuple[str, str]] = set()
    for path in tracked_files():
        if (name := ecosystem(path)) is None:
            continue
        root = name == "github-actions" or path.parent == PurePosixPath(".")
        used.add((name, "/" if root else f"/{path.parent}"))
    return used


def dependabot_updates() -> list[dict[str, Any]]:
    updates: list[dict[str, Any]] = yaml.safe_load(
        (ROOT / ".github/dependabot.yml").read_text(encoding="utf-8")
    )["updates"]
    return updates


def test_dependabot_updates_every_ecosystem_the_repository_uses() -> None:
    covered = {
        (update["package-ecosystem"], directory)
        for update in dependabot_updates()
        for directory in update.get("directories", [update.get("directory")])
    }
    used = used_ecosystems()

    assert {name for name, _ in used} == {
        "docker",
        "docker-compose",
        "github-actions",
        "gomod",
        "npm",
        "terraform",
        "uv",
    }
    assert used - covered == set()


def test_every_dependabot_update_waits_out_a_cooldown_before_it_can_release() -> None:
    updates = dependabot_updates()

    assert updates
    assert [
        update["package-ecosystem"]
        for update in updates
        if update.get("cooldown", {}).get("default-days", 0) < 5
    ] == []
