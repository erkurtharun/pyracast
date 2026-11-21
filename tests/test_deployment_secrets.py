import base64
import io
import json
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from deploy import runtime_secrets
from deploy.runtime_secrets import (
    database_files,
    environment_values,
    migration_files,
    runtime_files,
    write_files,
)
from tests.support.runtime_environment import RUNTIME_ENV, RUNTIME_SECRET

RUNTIME_VERSION = "projects/pyracast-example/secrets/pyracast-runtime/versions/3"
DATABASE_VERSION = "projects/pyracast-example/secrets/pyracast-database/versions/1"
HOST = {
    "project_id": "pyracast-example",
    "runtime_secret_id": "projects/pyracast-example/secrets/pyracast-runtime",
    "runtime_secret_version": RUNTIME_VERSION,
    "database_secret_version": DATABASE_VERSION,
    "backup_bucket": "pyracast-example-pyracast-backups",
    "domain": "pyracast.example",
    "acme_email": "ops@pyracast.example",
}
VALUE = "s3cr3t-value-never-printed"
RUNTIME = {
    "PYRACAST_SECRET_KEY": VALUE,
    "PGUSER": "pyracast_app",
    "PGPASSWORD": "app",
    "RABBITMQ_DEFAULT_PASS": "broker",
}
DATABASE = {"POSTGRES_PASSWORD": "admin"}


@pytest.mark.parametrize(
    "payload", [{}, [], {"BAD=KEY": "x"}, {"KEY": 1}, {"KEY": "secret\nPGUSER=admin"}]
)
def test_secret_rejects_invalid_environment_without_disclosing_payload(payload: object) -> None:
    with pytest.raises(ValueError, match="Secret") as error:
        environment_values(payload)
    assert "secret" not in str(error.value)
    assert "PGUSER=admin" not in str(error.value)


def test_runtime_separates_broker_credentials_and_preserves_literals(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        runtime_files(
            {
                "PYRACAST_SECRET_KEY": "literal$VALUE#'value",
                "PGUSER": "runtime",
                "PGPASSWORD": "password",
                "RABBITMQ_DEFAULT_PASS": "broker@pass/$",
            }
        ),
    )
    runtime = (tmp_path / "runtime.env").read_text()
    broker = (tmp_path / "broker.env").read_text()
    assert "PYRACAST_SECRET_KEY=literal$VALUE#'value\n" in runtime
    assert "amqp://pyracast:broker%40pass%2F%24@rabbitmq:5672/%2F" in runtime
    assert "RABBITMQ_DEFAULT_PASS" not in runtime
    assert "PGPASSWORD" not in broker
    assert "PYRACAST_SECRET_KEY" not in broker


def test_the_deployment_check_proof_reads_the_runtime_environment_this_helper_writes() -> None:
    assert runtime_files(RUNTIME_SECRET)["runtime.env"] == RUNTIME_ENV


def test_runtime_rejects_missing_credentials() -> None:
    with pytest.raises(ValueError, match="missing required"):
        runtime_files({"PGUSER": "runtime"})


def test_migration_secret_cannot_override_application_configuration(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="only PGUSER and PGPASSWORD"):
        migration_files({"PGUSER": "owner", "PGPASSWORD": "pw", "PYRACAST_DEBUG": "1"})
    write_files(tmp_path, migration_files({"PGUSER": "owner", "PGPASSWORD": "pw"}))
    assert (tmp_path / "migration.env").read_text() == "PGPASSWORD=pw\nPGUSER=owner\n"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"POSTGRES_PASSWORD": ""},
        {"POSTGRES_PASSWORD": "admin", "PGPASSWORD": "owner"},
    ],
)
def test_database_secret_rejects_application_or_migration_credentials(payload: object) -> None:
    with pytest.raises(ValueError, match=r"Secret|Database secret"):
        database_files(payload)


def test_database_secret_is_separate_from_runtime(tmp_path: Path) -> None:
    write_files(tmp_path, database_files({"POSTGRES_PASSWORD": "bootstrap$secret"}))
    assert (tmp_path / "database.env").read_text() == "POSTGRES_PASSWORD=bootstrap$secret\n"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["database.env"]


def test_staged_directory_belongs_to_the_resolved_release() -> None:
    script = Path("/opt/pyracast/releases/abc-123456789012/deploy/runtime_secrets.py")

    assert runtime_secrets.staged_directory(script) == runtime_secrets.SECRETS / (
        "abc-123456789012"
    )


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


# An int answers with that HTTP error status.
Answer = dict[str, Any] | bytes | int | BaseException


def secret_answer(payload: object) -> dict[str, Any]:
    data = base64.b64encode(json.dumps(payload).encode()).decode()
    return {"payload": {"data": data}}


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://example.invalid", code, "refused", Message(), None)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(runtime_secrets, "time", fake)
    return fake


@pytest.fixture
def reports(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, object]]]:
    recorded: list[tuple[str, str, dict[str, object]]] = []

    def report(event: str, severity: str, **details: object) -> None:
        recorded.append((event, severity, details))

    monkeypatch.setattr(runtime_secrets, "report", report)
    return recorded


def serve(
    monkeypatch: pytest.MonkeyPatch, answers: dict[str, list[Answer]]
) -> list[tuple[str, float]]:
    requests: list[tuple[str, float]] = []

    def urlopen(request: urllib.request.Request, *, timeout: float) -> io.BytesIO:
        requests.append((request.full_url, timeout))
        url = urlsplit(request.full_url)
        queue = answers[str(url.hostname) + url.path.rsplit(":", 1)[0].rsplit("/versions", 1)[0]]
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, int):
            raise http_error(answer)
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, bytes):
            return io.BytesIO(answer)
        return io.BytesIO(json.dumps(answer).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return requests


IDENTITY = "metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
RUNTIME_RESOURCE = (
    "secretmanager.googleapis.com/v1/projects/pyracast-example/secrets/pyracast-runtime"
)
DATABASE_RESOURCE = (
    "secretmanager.googleapis.com/v1/projects/pyracast-example/secrets/pyracast-database"
)


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(runtime_secrets, "SECRETS", tmp_path / "secrets")
    path = tmp_path / "host.json"
    path.write_text(json.dumps(HOST), encoding="utf-8")
    return path


def staged(tmp_path: Path) -> Path:
    return tmp_path / "secrets" / Path(runtime_secrets.__file__).resolve().parents[1].name


@pytest.mark.usefixtures("clock", "reports")
def test_secrets_are_staged_for_the_release_whose_helper_runs_with_one_identity_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: Path
) -> None:
    requests = serve(
        monkeypatch,
        {
            IDENTITY: [{"access_token": "instance-token"}],
            RUNTIME_RESOURCE: [secret_answer(RUNTIME)],
            DATABASE_RESOURCE: [secret_answer(DATABASE)],
        },
    )
    migration = tmp_path / "migration.json"
    migration.write_text(json.dumps({"PGUSER": "pyracast_owner", "PGPASSWORD": "owner"}))

    assert runtime_secrets.main([str(host)]) == 0
    assert runtime_secrets.main([str(host), "--migration", str(migration)]) == 0

    assert sorted(path.name for path in staged(tmp_path).iterdir()) == [
        "broker.env",
        "database.env",
        "migration.env",
        "runtime.env",
    ]
    assert [url for url, _ in requests] == [
        f"http://{IDENTITY}",
        f"https://{RUNTIME_RESOURCE}/versions/3:access",
        f"https://{DATABASE_RESOURCE}/versions/1:access",
    ]


def test_a_secret_is_read_with_the_instance_identity_from_its_pinned_version(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    seen: list[tuple[str, dict[str, str]]] = []

    def urlopen(request: urllib.request.Request, *, timeout: float) -> io.BytesIO:
        seen.append((request.full_url, dict(request.header_items())))
        if "metadata" in request.full_url:
            return io.BytesIO(json.dumps({"access_token": "instance-token"}).encode())
        return io.BytesIO(json.dumps(secret_answer(DATABASE)).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    deadline = runtime_secrets.Deadline(clock.monotonic() + runtime_secrets.RETRY_SECONDS)

    token = runtime_secrets.instance_token(deadline)

    assert runtime_secrets.access_secret(DATABASE_VERSION, token, "database", deadline) == DATABASE
    assert seen[0][1] == {"Metadata-flavor": "Google"}
    assert seen[1][1] == {"Authorization": "Bearer instance-token"}


@pytest.mark.usefixtures("reports")
def test_a_transient_answer_is_retried_with_backoff_until_it_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: Path, clock: Clock
) -> None:
    serve(
        monkeypatch,
        {
            IDENTITY: [urllib.error.URLError("unreachable"), {"access_token": "instance-token"}],
            RUNTIME_RESOURCE: [
                503,
                429,
                TimeoutError(),
                secret_answer(RUNTIME),
            ],
            DATABASE_RESOURCE: [secret_answer(DATABASE)],
        },
    )

    assert runtime_secrets.main([str(host)]) == 0

    assert clock.sleeps == [1.0, 1.0, 2.0, 4.0]
    assert (staged(tmp_path) / "runtime.env").exists()


def assert_refused(
    capsys: pytest.CaptureFixture[str],
    reports: list[tuple[str, str, dict[str, object]]],
    category: str,
    secret: str,
    message: str,
) -> None:
    error = capsys.readouterr().err
    assert error.startswith(f"Secret preparation failed ({category}): {message}")
    assert error.count("\n") == 1
    assert VALUE not in error
    assert reports == [
        ("pyracast_secrets_failed", "ERROR", {"category": category, "secret": secret})
    ]


@pytest.mark.parametrize(
    ("answers", "category", "secret", "message"),
    [
        pytest.param(
            {IDENTITY: [404]},
            "identity",
            "host",
            "the metadata server gave no access token",
            id="identity",
        ),
        pytest.param(
            {IDENTITY: [{"token": "no access token"}]},
            "identity",
            "host",
            "the metadata server gave no access token",
            id="identity-answer",
        ),
        pytest.param(
            {IDENTITY: [{"access_token": ""}]},
            "identity",
            "host",
            "the metadata server gave no access token",
            id="identity-empty",
        ),
        pytest.param(
            {IDENTITY: [{"access_token": "t"}], RUNTIME_RESOURCE: [b"<html>proxy</html>"]},
            "format",
            "runtime",
            "the runtime secret is not a JSON object of environment strings",
            id="format-answer",
        ),
        pytest.param(
            {IDENTITY: [{"access_token": "t"}], RUNTIME_RESOURCE: [403]},
            "permission",
            "runtime",
            "Secret Manager refused the runtime secret to the VM's service account",
            id="permission",
        ),
        pytest.param(
            {IDENTITY: [{"access_token": "t"}], RUNTIME_RESOURCE: [404]},
            "version",
            "runtime",
            "the pinned runtime secret version does not exist or is disabled",
            id="version",
        ),
        pytest.param(
            {IDENTITY: [{"access_token": "t"}], RUNTIME_RESOURCE: [urllib.error.URLError("down")]},
            "unavailable",
            "runtime",
            "Secret Manager did not answer for the runtime secret within 60 s",
            id="unavailable",
        ),
        pytest.param(
            {IDENTITY: [urllib.error.URLError("down")]},
            "unavailable",
            "host",
            "the metadata server did not answer within 60 s",
            id="metadata-unavailable",
        ),
        pytest.param(
            {
                IDENTITY: [{"access_token": "t"}],
                RUNTIME_RESOURCE: [secret_answer({**RUNTIME, "PGPASSWORD": ""})],
            },
            "format",
            "runtime",
            "the runtime secret is not a JSON object of environment strings",
            id="format-keys",
        ),
        pytest.param(
            {IDENTITY: [{"access_token": "t"}], RUNTIME_RESOURCE: [{"payload": {"data": VALUE}}]},
            "format",
            "runtime",
            "the runtime secret is not a JSON object of environment strings",
            id="format-payload",
        ),
        pytest.param(
            {
                IDENTITY: [{"access_token": "t"}],
                RUNTIME_RESOURCE: [secret_answer(RUNTIME)],
                DATABASE_RESOURCE: [secret_answer({"POSTGRES_PASSWORD": VALUE, "PGUSER": "x"})],
            },
            "format",
            "database",
            "the database secret is not a JSON object of environment strings",
            id="format-database",
        ),
    ],
)
@pytest.mark.usefixtures("clock")
def test_each_failure_is_reported_by_category_in_one_line_without_a_value(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    host: Path,
    reports: list[tuple[str, str, dict[str, object]]],
    answers: dict[str, list[Answer]],
    category: str,
    secret: str,
    message: str,
) -> None:
    serve(monkeypatch, answers)

    assert runtime_secrets.main([str(host)]) == 1

    assert_refused(capsys, reports, category, secret, message)


@pytest.mark.usefixtures("clock")
def test_a_refused_host_configuration_is_a_configuration_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    reports: list[tuple[str, str, dict[str, object]]],
) -> None:
    path = tmp_path / "host.json"
    path.write_text(json.dumps({**HOST, "runtime_secret_version": "latest"}), encoding="utf-8")
    serve(monkeypatch, {})

    assert runtime_secrets.main([str(path)]) == 1

    assert_refused(
        capsys,
        reports,
        "configuration",
        "host",
        "Host setting runtime_secret_version must be a numeric version of runtime_secret_id.",
    )


@pytest.mark.parametrize(
    ("content", "category", "message"),
    [
        (None, "configuration", "the migration secret file cannot be read"),
        (json.dumps({"PGUSER": VALUE}), "format", "the migration secret is not a JSON object"),
        ("{", "format", "the migration secret is not a JSON object"),
    ],
)
@pytest.mark.usefixtures("clock")
def test_a_migration_secret_that_cannot_be_read_or_is_malformed_is_refused_by_category(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    host: Path,
    reports: list[tuple[str, str, dict[str, object]]],
    content: str | None,
    category: str,
    message: str,
) -> None:
    migration = tmp_path / "migration.json"
    if content is not None:
        migration.write_text(content, encoding="utf-8")

    assert runtime_secrets.main([str(host), "--migration", str(migration)]) == 1

    assert_refused(capsys, reports, category, "migration", message)


@pytest.mark.usefixtures("clock")
def test_files_that_cannot_be_written_are_a_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    host: Path,
    reports: list[tuple[str, str, dict[str, object]]],
) -> None:
    migration = tmp_path / "migration.json"
    migration.write_text(json.dumps({"PGUSER": "pyracast_owner", "PGPASSWORD": VALUE}))

    def refuse(_directory: Path, _files: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(runtime_secrets, "write_files", refuse)

    assert runtime_secrets.main([str(host), "--migration", str(migration)]) == 1

    assert_refused(
        capsys, reports, "write", "migration", "the environment files of the release could not"
    )


@pytest.mark.usefixtures("clock")
def test_host_files_that_cannot_be_written_are_a_write_failure_of_the_host_secrets(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    host: Path,
    reports: list[tuple[str, str, dict[str, object]]],
) -> None:
    serve(
        monkeypatch,
        {
            IDENTITY: [{"access_token": "instance-token"}],
            RUNTIME_RESOURCE: [secret_answer(RUNTIME)],
            DATABASE_RESOURCE: [secret_answer(DATABASE)],
        },
    )

    def refuse(_directory: Path, _files: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(runtime_secrets, "write_files", refuse)

    assert runtime_secrets.main([str(host)]) == 1

    assert_refused(
        capsys, reports, "write", "host", "the environment files of the release could not"
    )


@pytest.mark.usefixtures("clock")
def test_an_unexpected_error_names_only_its_type(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    host: Path,
    reports: list[tuple[str, str, dict[str, object]]],
) -> None:
    def broken(_deadline: object) -> str:
        raise RuntimeError(VALUE)

    monkeypatch.setattr(runtime_secrets, "instance_token", broken)

    assert runtime_secrets.main([str(host)]) == 1

    assert_refused(capsys, reports, "unexpected", "host", "RuntimeError while preparing")
