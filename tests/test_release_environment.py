import json
from pathlib import Path
from typing import Any

import pytest

from deploy import host_contract, release_environment

IMAGES = [
    "us-docker.pkg.dev/project/repo/backend@sha256:" + "0" * 64,
    "us-docker.pkg.dev/project/repo/web@sha256:" + "1" * 64,
    "us-docker.pkg.dev/project/repo/gateway@sha256:" + "2" * 64,
    "us-docker.pkg.dev/project/repo/postgres@sha256:" + "3" * 64,
    "us-docker.pkg.dev/project/repo/rabbitmq@sha256:" + "4" * 64,
]
SECRETS = "/run/pyracast/releases/abc-123"
AUTHORITY = (Path(__file__).resolve().parents[1] / "deploy" / "origin-pull-off.pem").read_text(
    encoding="utf-8"
)
HOST = {
    "project_id": "pyracast-example",
    "runtime_secret_id": "projects/pyracast-example/secrets/pyracast-runtime",
    "runtime_secret_version": "projects/pyracast-example/secrets/pyracast-runtime/versions/1",
    "database_secret_version": "projects/pyracast-example/secrets/pyracast-database/versions/1",
    "backup_bucket": "pyracast-example-pyracast-backups",
    "domain": "pyracast.example",
    "acme_email": "ops@pyracast.example",
}


def settings(**changes: Any) -> dict[str, Any]:
    return {**HOST, **changes}


def config(**changes: Any) -> host_contract.HostConfig:
    return host_contract.parse(settings(**changes))


def test_the_release_environment_names_the_images_the_site_and_the_staged_secrets(
    tmp_path: Path,
) -> None:
    assert release_environment.render(config(), SECRETS, IMAGES, tmp_path) == {
        "BACKEND_IMAGE": IMAGES[0],
        "WEB_IMAGE": IMAGES[1],
        "GATEWAY_IMAGE": IMAGES[2],
        "POSTGRES_IMAGE": IMAGES[3],
        "RABBITMQ_IMAGE": IMAGES[4],
        "SITE_ADDRESS": "pyracast.example",
        "ACME_EMAIL": "ops@pyracast.example",
        "RUNTIME_ENV_FILE": f"{SECRETS}/runtime.env",
        "BROKER_ENV_FILE": f"{SECRETS}/broker.env",
        "DATABASE_ENV_FILE": f"{SECRETS}/database.env",
        "MIGRATION_ENV_FILE": f"{SECRETS}/migration.env",
        "ORIGIN_PULL": "off",
    }
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("value", [None, ""])
def test_a_host_without_an_origin_pull_authority_accepts_any_edge_handshake(
    tmp_path: Path, value: str | None
) -> None:
    changes = {} if value is None else {"origin_pull_ca": value}

    values = release_environment.render(config(**changes), SECRETS, IMAGES, tmp_path)

    assert values["ORIGIN_PULL"] == "off"
    assert "ORIGIN_PULL_CA" not in values


@pytest.mark.parametrize("separator", ["", "\n"])
def test_a_host_that_names_its_origin_pull_authority_requires_the_edge_certificate(
    tmp_path: Path, separator: str
) -> None:
    authority = AUTHORITY + separator + AUTHORITY

    values = release_environment.render(config(origin_pull_ca=authority), SECRETS, IMAGES, tmp_path)

    stored = tmp_path / release_environment.ORIGIN_PULL_CA
    assert values["ORIGIN_PULL"] == "required"
    assert values["ORIGIN_PULL_CA"] == stored.as_posix()
    assert stored.read_text(encoding="utf-8") == authority.strip() + "\n"


@pytest.mark.parametrize(
    ("secrets", "images"),
    [("/run/pyracast/releases/abc 123", IMAGES), (SECRETS, [*IMAGES[:4], "rabbitmq\nX=1"])],
)
def test_a_value_compose_would_misread_is_refused(
    tmp_path: Path, secrets: str, images: list[str]
) -> None:
    with pytest.raises(ValueError, match="Invalid nonsecret deployment configuration"):
        release_environment.render(config(), secrets, images, tmp_path)


def test_main_writes_the_release_environment_file(tmp_path: Path) -> None:
    host = tmp_path / "host.json"
    host.write_text(json.dumps(settings(origin_pull_ca=AUTHORITY)), encoding="utf-8")
    deploy = tmp_path / "release" / "deploy"
    deploy.mkdir(parents=True)

    assert release_environment.main([str(host), str(deploy / "release.env"), SECRETS, *IMAGES]) == 0

    lines = (deploy / "release.env").read_text(encoding="utf-8").splitlines()
    assert lines[0] == f"BACKEND_IMAGE={IMAGES[0]}"
    assert lines[4] == f"RABBITMQ_IMAGE={IMAGES[4]}"
    assert "ORIGIN_PULL=required" in lines
    assert f"ORIGIN_PULL_CA={(deploy / release_environment.ORIGIN_PULL_CA).as_posix()}" in lines


@pytest.mark.parametrize(
    ("host", "refusal"),
    [
        (
            {key: value for key, value in HOST.items() if key != "domain"},
            "Host setting domain is missing; it is required.",
        ),
        (settings(origin_pull_ca="not a certificate"), "Host setting origin_pull_ca must hold"),
        (settings(zone="europe-west1-b"), "Host setting zone is unknown"),
    ],
)
def test_a_refused_host_configuration_ends_the_release_in_one_line_before_anything_is_written(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    host: dict[str, Any],
    refusal: str,
) -> None:
    path = tmp_path / "host.json"
    path.write_text(json.dumps(host), encoding="utf-8")
    deploy = tmp_path / "release" / "deploy"
    deploy.mkdir(parents=True)

    status = release_environment.main([str(path), str(deploy / "release.env"), SECRETS, *IMAGES])

    error = capsys.readouterr().err
    assert status == 2
    assert error.startswith(f"Release refused before maintenance: {refusal}")
    assert error.count("\n") == 1
    assert list(deploy.iterdir()) == []


def test_main_refuses_a_release_that_names_only_four_images(tmp_path: Path) -> None:
    host = tmp_path / "host.json"
    host.write_text(json.dumps(settings()), encoding="utf-8")

    with pytest.raises(SystemExit):
        release_environment.main([str(host), str(tmp_path / "release.env"), SECRETS, *IMAGES[:4]])
    assert not (tmp_path / "release.env").exists()
