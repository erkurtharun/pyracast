import base64
import getpass
import io
import json
import urllib.request
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from deploy import create_secrets, runtime_secrets
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


def test_a_secret_is_read_with_the_instance_identity_from_its_pinned_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, dict[str, str], float]] = []
    payload = base64.b64encode(json.dumps({"POSTGRES_PASSWORD": "admin"}).encode()).decode()
    answers = {
        "metadata.google.internal": {"access_token": "instance-token"},
        "secretmanager.googleapis.com": {"payload": {"data": payload}},
    }

    def urlopen(request: urllib.request.Request, *, timeout: float) -> io.BytesIO:
        requests.append((request.full_url, dict(request.header_items()), timeout))
        host = urlsplit(request.full_url).hostname
        return io.BytesIO(json.dumps(answers[str(host)]).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    assert runtime_secrets.access_secret("projects/p/secrets/database/versions/2") == {
        "POSTGRES_PASSWORD": "admin"
    }
    assert [(url, timeout) for url, _, timeout in requests] == [
        (
            "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/"
            "default/token",
            10,
        ),
        (
            "https://secretmanager.googleapis.com/v1/projects/p/secrets/database/versions/2:access",
            15,
        ),
    ]
    assert requests[0][1] == {"Metadata-flavor": "Google"}
    assert requests[1][1] == {"Authorization": "Bearer instance-token"}


@pytest.mark.parametrize(
    "resource",
    [
        "projects/p/secrets/runtime/versions/latest",
        "projects/p/secrets/runtime",
        "projects/P/secrets/runtime/versions/1",
    ],
)
def test_a_secret_must_name_a_numeric_version(
    monkeypatch: pytest.MonkeyPatch, resource: str
) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)

    with pytest.raises(ValueError, match="numeric Secret Manager version"):
        runtime_secrets.access_secret(resource)


def forbidden(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("This step must not run.")


def answer(*replies: str) -> Callable[[str], str]:
    queue = list(replies)
    return lambda _prompt: queue.pop(0)


@pytest.mark.parametrize("backend", ["gmail", "smtp"])
def test_secret_creation_writes_three_private_files_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    backend: str,
) -> None:
    replies = [backend, ""] + (["smtp.example", "mailer"] if backend == "smtp" else [])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("builtins.input", answer(*replies))
    monkeypatch.setattr(getpass, "getpass", lambda _prompt: "smtp-password")

    create_secrets.main()

    secrets = tmp_path / "secrets"
    runtime = json.loads((secrets / "runtime.json").read_text(encoding="utf-8"))
    migration = json.loads((secrets / "migration.json").read_text(encoding="utf-8"))
    database = json.loads((secrets / "database.json").read_text(encoding="utf-8"))
    assert runtime["PYRACAST_EMAIL_BACKEND"] == backend
    assert runtime["PYRACAST_FROM_EMAIL"] == "info@pyracast.app"
    assert ("PYRACAST_EMAIL_PASSWORD" in runtime) is (backend == "smtp")
    assert runtime["PGUSER"] == "pyracast_app"
    assert migration["PGUSER"] == "pyracast_owner"
    assert len({runtime["PGPASSWORD"], migration["PGPASSWORD"], database["POSTGRES_PASSWORD"]}) == 3
    assert ("authorize_gmail.py" in capsys.readouterr().out) is (backend == "gmail")
    with pytest.raises(SystemExit, match="already exist"):
        create_secrets.main()


def test_secret_creation_offers_only_the_supported_mail_backends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("builtins.input", answer("sendmail"))

    with pytest.raises(SystemExit, match="Choose gmail or smtp"):
        create_secrets.main()
    assert list((tmp_path / "secrets").iterdir()) == []


def test_secret_creation_refuses_an_empty_database_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("builtins.input", answer("", ""))
    monkeypatch.setattr(create_secrets.secrets, "token_urlsafe", lambda _size: "")

    with pytest.raises(SystemExit, match="cannot be empty"):
        create_secrets.main()
    assert list((tmp_path / "secrets").iterdir()) == []
