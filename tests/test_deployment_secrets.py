import json
from pathlib import Path

import pytest

from deploy import runtime_secrets
from deploy.runtime_secrets import (
    environment_values,
    prepare_database,
    prepare_migration,
    prepare_runtime,
)


@pytest.mark.parametrize(
    "payload", [{}, [], {"BAD=KEY": "x"}, {"KEY": 1}, {"KEY": "secret\nPGUSER=admin"}]
)
def test_secret_rejects_invalid_environment_without_disclosing_payload(payload: object) -> None:
    with pytest.raises(ValueError, match="Secret") as error:
        environment_values(payload)
    assert "secret" not in str(error.value)


def test_runtime_separates_broker_credentials_and_preserves_literals(tmp_path: Path) -> None:
    prepare_runtime(
        {
            "PYRACAST_SECRET_KEY": "literal$VALUE#'value",
            "PGUSER": "runtime",
            "PGPASSWORD": "password",
            "RABBITMQ_DEFAULT_PASS": "broker@pass/$",
        },
        tmp_path,
    )
    runtime = (tmp_path / "runtime.env").read_text()
    broker = (tmp_path / "broker.env").read_text()
    assert "PYRACAST_SECRET_KEY=literal$VALUE#'value\n" in runtime
    assert "amqp://pyracast:broker%40pass%2F%24@rabbitmq:5672/%2F" in runtime
    assert "RABBITMQ_DEFAULT_PASS" not in runtime
    assert "PGPASSWORD" not in broker
    assert "PYRACAST_SECRET_KEY" not in broker


def test_runtime_rejects_missing_credentials(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing required"):
        prepare_runtime({"PGUSER": "runtime"}, tmp_path)
    assert not list(tmp_path.iterdir())


def test_migration_secret_cannot_override_application_configuration(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="only PGUSER and PGPASSWORD"):
        prepare_migration({"PGUSER": "owner", "PGPASSWORD": "pw", "PYRACAST_DEBUG": "1"}, tmp_path)
    prepare_migration({"PGUSER": "owner", "PGPASSWORD": "pw"}, tmp_path)
    assert (tmp_path / "migration.env").read_text() == "PGPASSWORD=pw\nPGUSER=owner\n"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"POSTGRES_PASSWORD": ""},
        {"POSTGRES_PASSWORD": "admin", "PGPASSWORD": "owner"},
    ],
)
def test_database_secret_rejects_application_or_migration_credentials(
    tmp_path: Path,
    payload: object,
) -> None:
    with pytest.raises(ValueError, match=r"Secret|Database secret"):
        prepare_database(payload, tmp_path)
    assert not list(tmp_path.iterdir())


def test_database_secret_is_separate_from_runtime(tmp_path: Path) -> None:
    prepare_database({"POSTGRES_PASSWORD": "bootstrap$secret"}, tmp_path)
    assert (tmp_path / "database.env").read_text() == "POSTGRES_PASSWORD=bootstrap$secret\n"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["database.env"]


def test_secrets_are_staged_for_the_release_whose_helper_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = {
        "projects/p/secrets/runtime/versions/3": {
            "PYRACAST_SECRET_KEY": "key",
            "PGUSER": "pyracast_app",
            "PGPASSWORD": "app",
            "RABBITMQ_DEFAULT_PASS": "broker",
        },
        "projects/p/secrets/database/versions/1": {"POSTGRES_PASSWORD": "admin"},
    }
    monkeypatch.setattr(runtime_secrets, "SECRETS", tmp_path / "secrets")
    monkeypatch.setattr(runtime_secrets, "access_secret", secrets.__getitem__)
    config = tmp_path / "host.json"
    config.write_text(
        json.dumps(
            {
                "runtime_secret_version": "projects/p/secrets/runtime/versions/3",
                "database_secret_version": "projects/p/secrets/database/versions/1",
            }
        )
    )
    migration = tmp_path / "migration.json"
    migration.write_text(json.dumps({"PGUSER": "pyracast_owner", "PGPASSWORD": "owner"}))

    runtime_secrets.main([str(config)])
    runtime_secrets.main([str(config), "--migration", str(migration)])

    release = Path(runtime_secrets.__file__).resolve().parents[1].name
    staged = tmp_path / "secrets" / release
    assert sorted(path.name for path in staged.iterdir()) == [
        "broker.env",
        "database.env",
        "migration.env",
        "runtime.env",
    ]
    assert sorted(path.name for path in (tmp_path / "secrets").iterdir()) == [release]


def test_staged_directory_belongs_to_the_resolved_release() -> None:
    script = Path("/opt/pyracast/releases/abc-123456789012/deploy/runtime_secrets.py")

    assert runtime_secrets.staged_directory(script) == runtime_secrets.SECRETS / (
        "abc-123456789012"
    )
