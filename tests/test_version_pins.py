import os
import re
import shlex
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
import yaml

from scripts import start_postgres
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
    "deploy/rabbitmq/Dockerfile",
    "deploy/*.yaml",
    "deploy/*.sh",
    ".github/workflows/*.yml",
    "scripts/*.py",
    "README.md",
)
IMAGES: dict[str, tuple[str, tuple[str, ...]]] = {
    "BuildKit": (
        "moby/buildkit",
        (".github/workflows/ci.yml", ".github/workflows/image-rescan.yml"),
    ),
    "Dockerfile syntax": (
        "docker/dockerfile",
        ("Dockerfile", "web/Dockerfile", "deploy/gateway/Dockerfile"),
    ),
    "Go": ("golang", ("deploy/gateway/Dockerfile",)),
    "Node.js": ("node", ("web/Dockerfile",)),
    "PostgreSQL in development": ("postgres", ("scripts/start_postgres.py",)),
    "PostgreSQL in production and CI": ("postgres", ("deploy/postgres/Dockerfile",)),
    "Python": ("python", ("Dockerfile", ".github/workflows/ci.yml", "deploy/scan-images.sh")),
    "RabbitMQ": (
        "rabbitmq",
        ("deploy/rabbitmq/Dockerfile", ".github/workflows/ci.yml", "README.md"),
    ),
    "RabbitMQ of the releases before the built broker": (
        "rabbitmq",
        ("deploy/test-broker-rollback.sh",),
    ),
    "SBOM scanner": ("docker/buildkit-syft-scanner", (".github/workflows/ci.yml",)),
    "Skopeo": ("quay.io/containers/skopeo", ("deploy/oci-images.sh",)),
    "Trivy": ("aquasec/trivy", ("deploy/scan-images.sh",)),
    "actionlint": ("rhysd/actionlint", (".github/workflows/ci.yml",)),
    "distroless C runtime": ("gcr.io/distroless/cc-debian13", ("Dockerfile", "web/Dockerfile")),
    "distroless static runtime": (
        "gcr.io/distroless/static-debian13",
        ("deploy/gateway/Dockerfile",),
    ),
    "uv": ("ghcr.io/astral-sh/uv", ("Dockerfile",)),
}


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
    "PostgreSQL in production and CI": [
        Text("deploy/postgres/Dockerfile", r"FROM postgres:(\d+\.\d+)-"),
        Text("scripts/start_postgres.py", r'^THROWAWAY_ARCHIVE_VERSION = "(\d+\.\d+)"$'),
        Text("docs/DEPLOYMENT.md", r"PostgreSQL (\d+\.\d+) on Alpine"),
        Text("docs/STACK.md", r"PostgreSQL (\d+\.\d+) \(production image and CI tests\)"),
        Text("docs/ARCHITECTURE.md", r"CI tests run production's (\d+\.\d+)"),
    ],
    "PostgreSQL in development": [
        Text("scripts/start_postgres.py", r'"postgres:(\d+\.\d+)@'),
        Text("scripts/start_postgres.py", r'^ARCHIVE_VERSION = "(\d+\.\d+)"$'),
        Text("docs/ARCHITECTURE.md", r"PostgreSQL (\d+\.\d+) is the verified"),
        Text("docs/STACK.md", r"and (\d+\.\d+) \(development cluster\)"),
    ],
    "RabbitMQ": [
        Text("deploy/rabbitmq/Dockerfile", rf"^FROM rabbitmq:{VERSION}@"),
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
    major = single("PostgreSQL in production and CI").split(".")[0]
    start_postgres = (ROOT / "scripts/start_postgres.py").read_text(encoding="utf-8")

    assert single("PostgreSQL in development").split(".")[0] == major
    assert f'"/var/lib/postgresql/{major}/docker"' in start_postgres


def apk_commands(dockerfile: str) -> list[list[str]]:
    commands = []
    for instruction in dockerfile.replace("\\\n", " ").splitlines():
        run = re.fullmatch(r"\s*RUN\s+(.*)", instruction, re.I)
        if run:
            command = list(shlex.shlex(run[1], posix=True, punctuation_chars=True))
            if command[:1] == ["apk"]:
                commands.append(command)
    return commands


def uncached_pinned_install(command: list[str]) -> bool:
    packages = command[3:]
    return (
        command[:3] == ["apk", "add", "--no-cache"]
        and bool(packages)
        and all(re.fullmatch(r"[a-z0-9][\w.+-]*>=\d[\w.]*-r\d+", package) for package in packages)
    )


@pytest.mark.parametrize(
    ("dockerfile", "commands"),
    [
        ("RUN apk add --no-cache 'a>=1-r0'", [["apk", "add", "--no-cache", "a>=1-r0"]]),
        ("run apk add b", [["apk", "add", "b"]]),
        ("RUN\tapk add b", [["apk", "add", "b"]]),
        ("  RUN apk \\\n  add b", [["apk", "add", "b"]]),
        ("RUN make apkindex", []),
        ("RUN echo apk", []),
        ("# RUN apk add b", []),
    ],
)
def test_apk_commands_are_run_instructions_whose_command_is_apk(
    dockerfile: str, commands: list[list[str]]
) -> None:
    assert apk_commands(dockerfile) == commands


@pytest.mark.parametrize(
    "command",
    [
        "apk add 'a>=1-r0'",
        "apk add --no-cache",
        "apk add --no-cache a",
        "apk add --no-cache --allow-untrusted 'a>=1-r0'",
        "apk add --no-cache 'a>=1-r0' --repository=https://example.com",
        "apk add --no-cache 'a>=1-r0' && apk upgrade",
        "apk upgrade --no-cache 'a>=1-r0'",
    ],
)
def test_an_install_outside_the_supported_form_is_refused(command: str) -> None:
    assert not uncached_pinned_install(*apk_commands(f"RUN {command}"))


def test_the_postgresql_image_adds_packages_uncached_at_a_quoted_minimum_version() -> None:
    commands = apk_commands((ROOT / "deploy/postgres/Dockerfile").read_text(encoding="utf-8"))

    assert commands
    for command in commands:
        assert uncached_pinned_install(command), command


def test_ci_clusters_run_the_production_postgresql_image() -> None:
    dockerfile = (ROOT / "deploy/postgres/Dockerfile").read_text(encoding="utf-8")
    production = re.search(r"^FROM (postgres:\S+)$", dockerfile, re.M)

    assert production
    assert start_postgres.production_image() == production[1]
    assert start_postgres.PRODUCTION_DOCKERFILE == ROOT / "deploy/postgres/Dockerfile"


def test_the_one_remaining_postgresql_difference_is_the_shared_development_cluster() -> None:
    # Running work shares the development cluster; it moves to production's minor deliberately.
    production = single("PostgreSQL in production and CI")
    development = single("PostgreSQL in development")
    development_locations = {location.path for location in PINS["PostgreSQL in development"]}

    assert {
        found.relative_to(ROOT).as_posix()
        for path in (*IMAGE_SOURCES, "docs/ARCHITECTURE.md", "docs/STACK.md")
        for found in ROOT.glob(path)
        if re.search(
            rf"postgres(?:ql)?:? ?{re.escape(development)}\b",
            found.read_text(encoding="utf-8"),
            re.I,
        )
    } <= development_locations
    assert release(development)[0] == release(production)[0]


def test_the_build_backend_range_admits_the_pinned_uv() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'"uv_build>=(\d+\.\d+\.\d+),<(\d+\.\d+)"', pyproject)
    assert match is not None
    uv = release(single("uv"))

    assert release(match[1]) <= uv
    assert uv[:2] < release(match[2])


def pinned_images() -> list[tuple[str, str, str, str]]:
    return [
        (match[1], match[2], match[3], path.relative_to(ROOT).as_posix())
        for pattern in IMAGE_SOURCES
        for path in sorted(ROOT.glob(pattern))
        for match in IMAGE.finditer(path.read_text(encoding="utf-8"))
    ]


def image_references(image: str, path: str) -> set[str]:
    text = (ROOT / path).read_text(encoding="utf-8")
    pattern = rf"(?<![\w./-]){re.escape(image)}(?::[A-Za-z0-9][\w.-]*)?(?:@[\w:]+)?"
    return set(re.findall(pattern, text)) - {image}


def test_every_image_tag_resolves_to_one_digest() -> None:
    digests: dict[tuple[str, str], set[str]] = defaultdict(set)
    for name, tag, digest, _ in pinned_images():
        digests[name, tag].add(digest)

    assert {image: found for image, found in digests.items() if len(found) != 1} == {}


def test_the_image_table_lists_every_location_of_a_pinned_image() -> None:
    assert {(name, path) for name, _, _, path in pinned_images()} == {
        (image, path) for image, paths in IMAGES.values() for path in paths
    }


@pytest.mark.parametrize("name", sorted(IMAGES))
def test_every_location_of_an_image_names_the_same_tag_and_digest(name: str) -> None:
    image, paths = IMAGES[name]
    found = {path: image_references(image, path) for path in paths}
    references = set[str]().union(*found.values())

    assert all(found.values()), found
    assert len(references) == 1, found
    (reference,) = references
    assert IMAGE.fullmatch(reference), found


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


# Dependency, build and cache directories hold manifests of other projects.
UNOWNED = frozenset(
    {
        ".dev",
        ".git",
        ".terraform",
        ".venv",
        "__pycache__",
        "htmlcov",
        "node_modules",
        "secrets",
        "staticfiles",
    }
)


def repository_files() -> list[PurePosixPath]:
    found: list[PurePosixPath] = []
    for directory, subdirectories, files in os.walk(ROOT):
        subdirectories[:] = [
            name
            for name in subdirectories
            if name not in UNOWNED and not name.startswith((".next", ".mypy", ".ruff", ".pytest"))
        ]
        relative = Path(directory).relative_to(ROOT).as_posix()
        found.extend(PurePosixPath(relative, name) for name in files)
    return found


def ecosystem(path: PurePosixPath) -> str | None:
    if path.parent.as_posix() == ".github/workflows":
        return "github-actions"
    if path.name == "go.mod":
        requires = re.search(r"^require\b", (ROOT / path).read_text(encoding="utf-8"), re.M)
        return "gomod" if requires else None
    if path.suffix == ".tf":
        return "terraform"
    if path.name == "compose.yaml":
        pins = IMAGE.search((ROOT / path).read_text(encoding="utf-8"))
        return "docker-compose" if pins else None
    manifests = {"Dockerfile": "docker", "package.json": "npm", "uv.lock": "uv"}
    return manifests.get(path.name)


def used_ecosystems() -> set[tuple[str, str]]:
    used: set[tuple[str, str]] = set()
    for path in repository_files():
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
        "github-actions",
        "gomod",
        "npm",
        "terraform",
        "uv",
    }
    assert used - covered == set()
    assert covered <= used


def test_dependabot_updates_the_base_image_of_each_support_image_the_release_builds() -> None:
    (docker,) = [
        update for update in dependabot_updates() if update["package-ecosystem"] == "docker"
    ]

    assert {"/deploy/postgres", "/deploy/rabbitmq"} <= set(docker["directories"])


def test_every_dependabot_update_waits_out_a_cooldown_before_it_can_release() -> None:
    updates = dependabot_updates()

    assert updates
    assert [
        update["package-ecosystem"]
        for update in updates
        if update.get("cooldown", {}).get("default-days", 0) < 5
    ] == []
