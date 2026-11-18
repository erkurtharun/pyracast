import json
from pathlib import Path
from typing import Any

import pytest

from deploy import release_environment

IMAGES = [
    "us-docker.pkg.dev/project/repo/backend@sha256:" + "0" * 64,
    "us-docker.pkg.dev/project/repo/web@sha256:" + "1" * 64,
    "us-docker.pkg.dev/project/repo/gateway@sha256:" + "2" * 64,
    "us-docker.pkg.dev/project/repo/postgres@sha256:" + "3" * 64,
]
SECRETS = "/run/pyracast/releases/abc-123"
AUTHORITY = (Path(__file__).resolve().parents[1] / "deploy" / "origin-pull-off.pem").read_text(
    encoding="utf-8"
)


def config(**changes: Any) -> dict[str, Any]:
    return {"domain": "pyracast.example", "acme_email": "ops@pyracast.example", **changes}


def test_the_release_environment_names_the_images_the_site_and_the_staged_secrets(
    tmp_path: Path,
) -> None:
    assert release_environment.render(config(), SECRETS, IMAGES, tmp_path) == {
        "BACKEND_IMAGE": IMAGES[0],
        "WEB_IMAGE": IMAGES[1],
        "GATEWAY_IMAGE": IMAGES[2],
        "POSTGRES_IMAGE": IMAGES[3],
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
    "value",
    [
        "not a certificate",
        AUTHORITY.replace("CERTIFICATE", "PRIVATE KEY"),
        AUTHORITY + "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n",
        AUTHORITY.replace("\n", "\n$", 1),
        ["certificate"],
        1,
    ],
)
def test_an_origin_pull_authority_that_is_not_only_certificates_is_refused(
    tmp_path: Path, value: object
) -> None:
    with pytest.raises(ValueError, match="origin_pull_ca"):
        release_environment.render(config(origin_pull_ca=value), SECRETS, IMAGES, tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "changes",
    [{"domain": "pyracast.example extra"}, {"acme_email": "ops@example.com\nX=1"}],
)
def test_a_value_compose_would_misread_is_refused(tmp_path: Path, changes: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="Invalid nonsecret deployment configuration"):
        release_environment.render(config(**changes), SECRETS, IMAGES, tmp_path)


def test_main_writes_the_release_environment_file(tmp_path: Path) -> None:
    host = tmp_path / "host.json"
    host.write_text(json.dumps(config(origin_pull_ca=AUTHORITY)), encoding="utf-8")
    deploy = tmp_path / "release" / "deploy"
    deploy.mkdir(parents=True)

    release_environment.main([str(host), str(deploy / "release.env"), SECRETS, *IMAGES])

    lines = (deploy / "release.env").read_text(encoding="utf-8").splitlines()
    assert lines[0] == f"BACKEND_IMAGE={IMAGES[0]}"
    assert "ORIGIN_PULL=required" in lines
    assert f"ORIGIN_PULL_CA={(deploy / release_environment.ORIGIN_PULL_CA).as_posix()}" in lines
