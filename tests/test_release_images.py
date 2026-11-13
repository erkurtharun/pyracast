import json
import subprocess
import sys
from pathlib import Path

import pytest

from deploy import pin_release_images

REPOSITORY = "us-central1-docker.pkg.dev/pyracast/pyracast"
IMAGES = {
    key: f"{REPOSITORY}/{package}@sha256:{'1' * 64}"
    for key, package in pin_release_images.IMAGE_PACKAGES.items()
}


@pytest.mark.parametrize(
    "values",
    [
        [],
        {"BACKEND_IMAGE": IMAGES["BACKEND_IMAGE"]},
        {**IMAGES, "SECRET_KEY": "must-not-be-read"},
        {**IMAGES, "POSTGRES_IMAGE": IMAGES["POSTGRES_IMAGE"].replace("pyracast/", "other/")},
        {**IMAGES, "POSTGRES_IMAGE": f"{REPOSITORY}/postgres:latest"},
        {**IMAGES, "POSTGRES_IMAGE": IMAGES["BACKEND_IMAGE"]},
    ],
)
def test_release_images_reject_unexpected_or_mutable_identities(values: object) -> None:
    with pytest.raises(ValueError, match="release image"):
        pin_release_images.validate_images(json.dumps(values), REPOSITORY)


def test_current_release_reads_only_image_values_without_executing_environment(
    tmp_path: Path,
) -> None:
    current = tmp_path / "current"
    deploy = current / "deploy"
    deploy.mkdir(parents=True)
    source = "\n".join(f"{key}={value}" for key, value in IMAGES.items())
    (deploy / "release.env").write_text(source + "\nIGNORED=$(exit 99)\n")
    script = pin_release_images.CURRENT_IMAGES_SCRIPT.replace(
        "Path('/opt/pyracast/current')",
        f"Path({str(current)!r})",
    )
    result = subprocess.check_output([sys.executable, "-c", script], text=True)  # noqa: S603
    assert pin_release_images.validate_images(result, REPOSITORY) == IMAGES


def test_missing_current_release_is_distinct_from_a_broken_existing_release(tmp_path: Path) -> None:
    current = tmp_path / "current"
    script = pin_release_images.CURRENT_IMAGES_SCRIPT.replace(
        "Path('/opt/pyracast/current')",
        f"Path({str(current)!r})",
    )
    result = subprocess.check_output([sys.executable, "-c", script], text=True)  # noqa: S603
    assert json.loads(result) == {}
    current.mkdir()
    with pytest.raises(subprocess.CalledProcessError):
        subprocess.check_output([sys.executable, "-c", script], stderr=subprocess.DEVNULL)  # noqa: S603
    (current / "deploy").mkdir()
    (current / "deploy/release.env").write_text("SITE_ADDRESS=example.com\n")
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
        assert len(commands) == 4
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
