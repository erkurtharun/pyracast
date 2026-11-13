# ruff: noqa: S603, S607 -- Fixed executables and argument lists; remote source is fixed and shell-quoted.
import argparse
import json
import os
import re
import shlex
import subprocess

IMAGE_PACKAGES = {
    "BACKEND_IMAGE": "backend",
    "WEB_IMAGE": "web",
    "GATEWAY_IMAGE": "gateway",
    "POSTGRES_IMAGE": "postgres",
}
CURRENT_IMAGES_PREFIX = "PYRACAST_RELEASE_IMAGES="
CURRENT_IMAGES_SCRIPT = (
    f"record_prefix = {CURRENT_IMAGES_PREFIX!r}\n"
    + """
import json
from pathlib import Path
current = Path('/opt/pyracast/current')
try:
    current.lstat()
except FileNotFoundError:
    print(record_prefix + '{}')
else:
    wanted = {'BACKEND_IMAGE', 'WEB_IMAGE', 'GATEWAY_IMAGE', 'POSTGRES_IMAGE'}
    values = {}
    for line in (current / 'deploy/release.env').read_text().splitlines():
        key, separator, value = line.partition('=')
        if key in wanted:
            if not separator or key in values:
                raise ValueError('Invalid current release image configuration.')
            values[key] = value
    if values.keys() != wanted:
        raise ValueError('Current release is missing image identities.')
    print(record_prefix + json.dumps(values))
"""
)


def validate_images(payload: str, repository: str) -> dict[str, str]:
    values = json.loads(payload)
    if not isinstance(values, dict) or (values and set(values) != IMAGE_PACKAGES.keys()):
        raise ValueError("Expected exactly four release image identities.")
    for key, value in values.items():
        expected = rf"{re.escape(repository)}/{IMAGE_PACKAGES[key]}@sha256:[0-9a-f]{{64}}"
        if not isinstance(value, str) or re.fullmatch(expected, value) is None:
            raise ValueError(f"Invalid immutable release image: {key}.")
    return values


def parse_current_images(output: str, repository: str) -> dict[str, str]:
    records = [
        line.removeprefix(CURRENT_IMAGES_PREFIX)
        for line in output.splitlines()
        if line.startswith(CURRENT_IMAGES_PREFIX)
    ]
    if len(records) != 1:
        raise ValueError("Expected exactly one current release image record.")
    return validate_images(records[0], repository)


def current_images(project: str, zone: str, instance: str, repository: str) -> dict[str, str]:
    payload = subprocess.check_output(
        [
            "gcloud",
            "compute",
            "ssh",
            instance,
            f"--project={project}",
            f"--zone={zone}",
            "--tunnel-through-iap",
            "--ssh-key-expire-after=1h",
            "--quiet",
            "--command=sudo python3 -c " + shlex.quote(CURRENT_IMAGES_SCRIPT),
        ],
        text=True,
        timeout=120,
    )
    return parse_current_images(payload, repository)


def pin_images(images: dict[str, str], project: str) -> None:
    for image in images.values():
        package, _, digest = image.partition("@")
        tag = package + ":production"
        subprocess.run(
            [
                "gcloud",
                "artifacts",
                "docker",
                "tags",
                "add",
                image,
                tag,
                f"--project={project}",
                "--quiet",
            ],
            check=True,
            timeout=120,
        )
        actual = subprocess.check_output(
            ["docker", "buildx", "imagetools", "inspect", tag, "--format", "{{.Manifest.Digest}}"],
            text=True,
            timeout=120,
        ).strip()
        if actual != digest:
            raise ValueError(f"Production retention tag does not match {package}.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("release", choices=("current", "published"))
    arguments = parser.parse_args()
    project = os.environ["GCP_PROJECT_ID"]
    repository = os.environ["GCP_ARTIFACT_REPOSITORY"]
    if arguments.release == "current":
        images = current_images(
            project,
            os.environ["GCP_ZONE"],
            os.environ["GCP_INSTANCE"],
            repository,
        )
    else:
        images = validate_images(
            json.dumps({key: os.environ[key] for key in IMAGE_PACKAGES}),
            repository,
        )
    pin_images(images, project)
    print(f"Verified {len(images)} production image retention tags.")


if __name__ == "__main__":
    main()
