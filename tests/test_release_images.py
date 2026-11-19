import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from deploy import ci_images, pin_release_images, release_state

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "us-central1-docker.pkg.dev/pyracast/pyracast"
PACKAGES = {
    "BACKEND_IMAGE": "backend",
    "WEB_IMAGE": "web",
    "GATEWAY_IMAGE": "gateway",
    "POSTGRES_IMAGE": "postgres",
    "RABBITMQ_IMAGE": "rabbitmq",
}
IMAGES = {key: f"{REPOSITORY}/{package}@sha256:{'1' * 64}" for key, package in PACKAGES.items()}
# A release installed before the release built the broker image ran the public broker.
FOUR_IMAGES = {key: value for key, value in IMAGES.items() if key != "RABBITMQ_IMAGE"}
PUBLIC_BROKER = (
    "rabbitmq:4.4.1@sha256:183272aa2ff82718cabeef343de8d9a900931ea65fda4e563fed2a6d9efde578"
)


def test_a_release_names_five_images_each_published_as_its_own_package() -> None:
    assert pin_release_images.IMAGE_PACKAGES == PACKAGES
    assert tuple(PACKAGES) == release_state.IMAGE_KEYS
    assert tuple(PACKAGES.values()) == ci_images.IMAGES


def test_every_compose_service_runs_an_image_the_release_names() -> None:
    compose = yaml.safe_load((ROOT / "deploy/compose.yaml").read_text(encoding="utf-8"))
    named: set[str] = set()
    for service, settings in compose["services"].items():
        match = re.fullmatch(
            r"\$\{([A-Z]+_IMAGE):\?Set \1 to the verified digest\}", settings["image"]
        )
        assert match, service
        named.add(match[1])

    assert named == set(PACKAGES)


@pytest.mark.parametrize(
    ("script", "before", "after"),
    [
        ("scan-images.sh", "", ""),
        ("smoke.sh", "", ""),
        ("install-release.sh", "SHA ", " BUNDLE MIGRATION_JSON"),
    ],
)
def test_each_script_given_the_release_images_takes_every_one_in_release_order(
    script: str, before: str, after: str
) -> None:
    source = (ROOT / "deploy" / script).read_text(encoding="utf-8")
    count = len(before.split()) + len(PACKAGES) + len(after.split())

    assert f"{script} {before}{' '.join(PACKAGES)}{after}'" in source
    assert f"[[ $# == {count}" in source
    if not before:
        exported = " ".join(f"{key}=${index}" for index, key in enumerate(PACKAGES, start=1))
        assert f"export {exported}" in source


@pytest.mark.parametrize(
    "values",
    [
        [],
        {"BACKEND_IMAGE": IMAGES["BACKEND_IMAGE"]},
        {**IMAGES, "SECRET_KEY": "must-not-be-read"},
        {**IMAGES, "POSTGRES_IMAGE": IMAGES["POSTGRES_IMAGE"].replace("pyracast/", "other/")},
        {**IMAGES, "POSTGRES_IMAGE": f"{REPOSITORY}/postgres:latest"},
        {**IMAGES, "POSTGRES_IMAGE": IMAGES["BACKEND_IMAGE"]},
        {**IMAGES, "RABBITMQ_IMAGE": PUBLIC_BROKER},
        {key: value for key, value in IMAGES.items() if key != "POSTGRES_IMAGE"},
    ],
)
def test_release_images_reject_unexpected_or_mutable_identities(values: object) -> None:
    with pytest.raises(ValueError, match="release image"):
        pin_release_images.validate_images(json.dumps(values), REPOSITORY)


@pytest.mark.parametrize("images", [IMAGES, FOUR_IMAGES])
def test_current_release_reads_only_image_values_without_executing_environment(
    tmp_path: Path, images: dict[str, str]
) -> None:
    current = tmp_path / "current"
    deploy = current / "deploy"
    deploy.mkdir(parents=True)
    source = "\n".join(f"{key}={value}" for key, value in images.items())
    (deploy / "release.env").write_text(source + "\nIGNORED=$(exit 99)\n")
    script = pin_release_images.CURRENT_IMAGES_SCRIPT.replace(
        "Path('/opt/pyracast/current')",
        f"Path({str(current)!r})",
    )
    result = subprocess.check_output([sys.executable, "-c", script], text=True)  # noqa: S603
    assert pin_release_images.parse_current_images(result, REPOSITORY) == images


@pytest.mark.parametrize(
    "content",
    [
        "SITE_ADDRESS=example.com\n",
        "".join(f"{key}={value}\n" for key, value in IMAGES.items() if key != "POSTGRES_IMAGE"),
    ],
)
def test_missing_current_release_is_distinct_from_a_broken_existing_release(
    tmp_path: Path, content: str
) -> None:
    current = tmp_path / "current"
    script = pin_release_images.CURRENT_IMAGES_SCRIPT.replace(
        "Path('/opt/pyracast/current')",
        f"Path({str(current)!r})",
    )
    result = subprocess.check_output([sys.executable, "-c", script], text=True)  # noqa: S603
    assert pin_release_images.parse_current_images(result, REPOSITORY) == {}
    current.mkdir()
    with pytest.raises(subprocess.CalledProcessError):
        subprocess.check_output([sys.executable, "-c", script], stderr=subprocess.DEVNULL)  # noqa: S603
    (current / "deploy").mkdir()
    (current / "deploy/release.env").write_text(content)
    with pytest.raises(subprocess.CalledProcessError):
        subprocess.check_output([sys.executable, "-c", script], stderr=subprocess.DEVNULL)  # noqa: S603


def test_current_release_access_errors_abort_retention_pinning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def denied(command: list[str], **_kwargs: object) -> str:
        raise subprocess.CalledProcessError(255, command)

    monkeypatch.setattr(subprocess, "check_output", denied)
    with pytest.raises(subprocess.CalledProcessError):
        pin_release_images.current_images("pyracast", "us-central1-a", "pyracast", REPOSITORY)


@pytest.mark.parametrize("images", [IMAGES, {}])
def test_current_release_ignores_ssh_bootstrap_output_around_its_framed_record(
    monkeypatch: pytest.MonkeyPatch,
    images: dict[str, str],
) -> None:
    output = (
        "Generating public/private rsa key pair.\n"
        "Your identification has been saved in temporary-key.\n"
        "unrelated {banner} output\n"
        "PYRACAST_RELEASE_IMAGES=" + json.dumps(images) + "\r\n"
        "Connection closed.\n"
    )
    monkeypatch.setattr(subprocess, "check_output", lambda *_args, **_kwargs: output)
    assert (
        pin_release_images.current_images("pyracast", "us-central1-a", "pyracast", REPOSITORY)
        == images
    )


@pytest.mark.parametrize(
    "output",
    [
        "{}\n",
        "SSH banner only\n",
        " PYRACAST_RELEASE_IMAGES={}\n",
        "PYRACAST_RELEASE_IMAGES={}\nPYRACAST_RELEASE_IMAGES={}\n",
        "PYRACAST_RELEASE_IMAGES=not-json\n",
        'PYRACAST_RELEASE_IMAGES={"BACKEND_IMAGE":"mutable:latest"}\n',
    ],
)
def test_current_release_protocol_rejects_missing_ambiguous_or_invalid_records(output: str) -> None:
    with pytest.raises(
        ValueError, match=r"current release image record|Expecting value|release image"
    ):
        pin_release_images.parse_current_images(output, REPOSITORY)


@pytest.mark.parametrize("matching_digest", [True, False])
def test_production_retention_pins_require_the_selected_digest(
    monkeypatch: pytest.MonkeyPatch,
    matching_digest: bool,
) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    def inspect(command: list[str], **_kwargs: object) -> str:
        assert command[:4] == ["docker", "buildx", "imagetools", "inspect"]
        return "sha256:" + ("1" if matching_digest else "2") * 64

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "check_output", inspect)
    if matching_digest:
        pin_release_images.pin_images(IMAGES, "pyracast")
        assert len(commands) == len(IMAGES)
    else:
        with pytest.raises(ValueError, match="retention tag"):
            pin_release_images.pin_images(IMAGES, "pyracast")
        assert len(commands) == 1
    assert commands[0] == [
        "gcloud",
        "artifacts",
        "docker",
        "tags",
        "add",
        IMAGES["BACKEND_IMAGE"],
        f"{REPOSITORY}/backend:production",
        "--project=pyracast",
        "--quiet",
    ]


def workflow_environment(monkeypatch: pytest.MonkeyPatch, images: dict[str, str]) -> None:
    environment = {
        "GCP_PROJECT_ID": "pyracast",
        "GCP_ARTIFACT_REPOSITORY": REPOSITORY,
        "GCP_ZONE": "us-central1-a",
        "GCP_INSTANCE": "pyracast-vm",
        **images,
    }
    for key in PACKAGES:
        monkeypatch.delenv(key, raising=False)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize(
    ("release", "images"),
    [("current", IMAGES), ("current", FOUR_IMAGES), ("published", IMAGES)],
)
def test_main_pins_the_live_or_the_published_release_from_the_workflow_environment(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    release: str,
    images: dict[str, str],
) -> None:
    pinned: list[tuple[dict[str, str], str]] = []
    reads: list[tuple[str, ...]] = []

    def current(*arguments: str) -> dict[str, str]:
        reads.append(arguments)
        return images

    workflow_environment(monkeypatch, images if release == "published" else {})
    monkeypatch.setattr(pin_release_images, "current_images", current)
    monkeypatch.setattr(
        pin_release_images, "pin_images", lambda images, project: pinned.append((images, project))
    )
    monkeypatch.setattr(sys, "argv", ["pin_release_images.py", release])

    pin_release_images.main()

    assert pinned == [(images, "pyracast")]
    assert reads == (
        [("pyracast", "us-central1-a", "pyracast-vm", REPOSITORY)] if release == "current" else []
    )
    assert capsys.readouterr().out == f"Verified {len(images)} production image retention tags.\n"


def test_the_published_release_must_name_the_broker_image_it_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pinned: list[dict[str, str]] = []
    workflow_environment(monkeypatch, FOUR_IMAGES)
    monkeypatch.setattr(pin_release_images, "pin_images", lambda images, _: pinned.append(images))
    monkeypatch.setattr(sys, "argv", ["pin_release_images.py", "published"])

    with pytest.raises(KeyError, match="RABBITMQ_IMAGE"):
        pin_release_images.main()
    assert pinned == []
