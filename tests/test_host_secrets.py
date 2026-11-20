import getpass
import itertools
import json
import os
import secrets
from collections.abc import Callable
from pathlib import Path

import pytest

from deploy import host_secrets

FILES = ("runtime.json", "migration.json", "database.json")


@pytest.fixture
def generated(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    values: list[str] = []
    counter = itertools.count()

    def token(size: int) -> str:
        value = f"generated-{next(counter)}-{'x' * size}"
        values.append(value)
        return value

    monkeypatch.setattr(secrets, "token_urlsafe", token)
    return values


def answer(*replies: str) -> Callable[[str], str]:
    queue = list(replies)
    return lambda _prompt: queue.pop(0)


def run(directory: Path, *arguments: str) -> int:
    return host_secrets.main(["--directory", str(directory), *arguments])


def read(directory: Path, name: str) -> dict[str, str]:
    values: dict[str, str] = json.loads((directory / name).read_text(encoding="utf-8"))
    return values


def printed(capsys: pytest.CaptureFixture[str]) -> str:
    output = capsys.readouterr()
    return output.out + output.err


@pytest.fixture
def created(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, generated: list[str]) -> Path:
    monkeypatch.setattr("builtins.input", answer("smtp", "", "smtp.example.com", "mailer"))
    monkeypatch.setattr(getpass, "getpass", lambda _prompt: "smtp-password")
    assert run(tmp_path, "create") == 0
    return tmp_path


def test_the_default_directory_is_the_repositorys_ignored_secrets_directory() -> None:
    assert (
        Path(host_secrets.__file__).resolve().parents[1] / ("secrets")
        == host_secrets.DEFAULT_DIRECTORY
    )


@pytest.mark.parametrize("backend", ["gmail", "smtp"])
def test_create_writes_three_private_files_once_and_prints_no_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    generated: list[str],
    backend: str,
) -> None:
    replies = [backend, ""] + (["smtp.example.com", "mailer"] if backend == "smtp" else [])
    monkeypatch.setattr("builtins.input", answer(*replies))
    monkeypatch.setattr(getpass, "getpass", lambda _prompt: "smtp-password")

    assert run(tmp_path, "create") == 0

    runtime, migration, database = (read(tmp_path, name) for name in FILES)
    assert runtime["PYRACAST_EMAIL_BACKEND"] == backend
    assert runtime["PYRACAST_FROM_EMAIL"] == "info@pyracast.app"
    assert ("PYRACAST_EMAIL_PASSWORD" in runtime) is (backend == "smtp")
    assert runtime["PGUSER"] == "pyracast_app"
    assert migration["PGUSER"] == "pyracast_owner"
    assert len({runtime["PGPASSWORD"], migration["PGPASSWORD"], database["POSTGRES_PASSWORD"]}) == 3
    output = printed(capsys)
    assert ("authorize_gmail.py" in output) is (backend == "gmail")
    assert not any(value in output for value in [*generated, "smtp-password"])
    if os.name == "posix":
        assert {(tmp_path / name).stat().st_mode & 0o777 for name in FILES} == {0o600}

    assert run(tmp_path, "create") == 2
    assert "already exist" in printed(capsys)


@pytest.mark.parametrize(
    ("replies", "refusal"),
    [(("sendmail",), "Choose gmail or smtp."), (("smtp", "", ""), "Enter the SMTP host.")],
)
def test_create_refuses_an_unsupported_mail_setup_and_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    replies: tuple[str, ...],
    refusal: str,
) -> None:
    monkeypatch.setattr("builtins.input", answer(*replies))

    assert run(tmp_path, "create") == 2
    assert refusal in printed(capsys)
    assert list(tmp_path.iterdir()) == []


def test_create_removes_what_it_wrote_when_a_later_file_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, generated: list[str]
) -> None:
    monkeypatch.setattr("builtins.input", answer("gmail", ""))
    original = host_secrets.write_new
    written: list[Path] = []

    def failing(path: Path, values: dict[str, str]) -> None:
        if path.name == "database.json":
            raise OSError(28, "No space left on device")
        original(path, values)
        written.append(path)

    monkeypatch.setattr(host_secrets, "write_new", failing)

    with pytest.raises(OSError, match="No space"):
        run(tmp_path, "create")
    assert written
    assert list(tmp_path.iterdir()) == []


def test_check_accepts_a_complete_set_without_printing_a_value(
    created: Path, capsys: pytest.CaptureFixture[str], generated: list[str]
) -> None:
    capsys.readouterr()

    assert run(created, "check") == 0

    output = printed(capsys)
    assert output == "runtime.json, migration.json and database.json are complete.\n"


@pytest.mark.parametrize(
    ("name", "changes", "problem"),
    [
        ("runtime.json", {"PGPASSWORD": None}, "runtime.json: PGPASSWORD is missing."),
        ("runtime.json", {"PYRACAST_WEATHER": "1"}, "runtime.json: PYRACAST_WEATHER is not"),
        ("runtime.json", {"PGUSER": "postgres"}, "runtime.json: PGUSER must be pyracast_app."),
        (
            "runtime.json",
            {"PYRACAST_GOOGLE_CLIENT_ID": "client"},
            "runtime.json: set PYRACAST_GOOGLE_CLIENT_ID and PYRACAST_GOOGLE_CLIENT_SECRET",
        ),
        ("runtime.json", {"PYRACAST_FROM_EMAIL": "a\nb"}, "runtime.json: Secret contains"),
        ("migration.json", {"PGUSER": "pyracast_app"}, "migration.json: PGUSER must be"),
        ("migration.json", {"PYRACAST_DEBUG": "1"}, "migration.json: Migration secret must"),
        ("database.json", {"POSTGRES_PASSWORD": ""}, "database.json: Database secret must"),
    ],
)
def test_check_names_each_problem_by_file_and_key_only(
    created: Path,
    capsys: pytest.CaptureFixture[str],
    name: str,
    changes: dict[str, str | None],
    problem: str,
) -> None:
    values = {**read(created, name), **changes}
    (created / name).write_text(
        json.dumps({key: value for key, value in values.items() if value is not None}),
        encoding="utf-8",
    )
    capsys.readouterr()

    assert run(created, "check") == 1

    output = printed(capsys)
    assert problem in output
    assert "smtp-password" not in output


def test_check_refuses_a_shared_password_and_a_missing_file(
    created: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime = read(created, "runtime.json")
    migration = read(created, "migration.json")
    (created / "migration.json").write_text(
        json.dumps({**migration, "PGPASSWORD": runtime["PGPASSWORD"]}), encoding="utf-8"
    )
    (created / "database.json").unlink()
    capsys.readouterr()

    assert run(created, "check") == 1

    output = printed(capsys)
    assert "database.json is missing or not a JSON object." in output
    assert "runtime.json and migration.json must not share a database password." in output
    assert runtime["PGPASSWORD"] not in output


def test_rotating_the_secret_key_keeps_the_previous_key_as_the_first_fallback(
    created: Path, capsys: pytest.CaptureFixture[str], generated: list[str]
) -> None:
    before = read(created, "runtime.json")
    (created / "runtime.json").write_text(
        json.dumps({**before, "PYRACAST_SECRET_KEY_FALLBACKS": "older-key"}), encoding="utf-8"
    )
    capsys.readouterr()

    assert run(created, "rotate", "secret-key") == 0

    after = read(created, "runtime.json")
    assert after["PYRACAST_SECRET_KEY"] == generated[-1]
    assert after["PYRACAST_SECRET_KEY_FALLBACKS"] == f"{before['PYRACAST_SECRET_KEY']} older-key"
    output = printed(capsys)
    assert "reencrypt_secrets --verify" in output
    assert not any(value in output for value in [*generated, before["PYRACAST_SECRET_KEY"]])


@pytest.mark.parametrize(
    ("target", "name"), [("runtime-password", "runtime.json"), ("owner-password", "migration.json")]
)
def test_rotating_a_role_password_replaces_only_that_password(
    created: Path,
    capsys: pytest.CaptureFixture[str],
    generated: list[str],
    target: str,
    name: str,
) -> None:
    before = {file: read(created, file) for file in FILES}

    assert run(created, "rotate", target) == 0

    after = {file: read(created, file) for file in FILES}
    assert after[name]["PGPASSWORD"] == generated[-1]
    assert {**after[name], "PGPASSWORD": before[name]["PGPASSWORD"]} == before[name]
    assert {file: values for file, values in after.items() if file != name} == {
        file: values for file, values in before.items() if file != name
    }
    assert generated[-1] not in printed(capsys)


@pytest.mark.parametrize("target", ["broker-password", "database-password"])
def test_a_password_the_release_cannot_apply_is_not_rotated(
    created: Path, capsys: pytest.CaptureFixture[str], target: str
) -> None:
    before = {file: read(created, file) for file in FILES}

    assert run(created, "rotate", target) == 2

    assert "initializes a new data directory" in printed(capsys)
    assert {file: read(created, file) for file in FILES} == before


def test_set_stores_a_value_typed_without_echo_and_prints_only_the_key(
    created: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    typed = "epias-credential-typed"
    monkeypatch.setattr(getpass, "getpass", lambda _prompt: typed)
    capsys.readouterr()

    assert run(created, "set", "EPIAS_PASSWORD") == 0

    assert read(created, "runtime.json")["EPIAS_PASSWORD"] == typed
    assert printed(capsys) == "Stored EPIAS_PASSWORD in runtime.json.\n"


@pytest.mark.parametrize(
    ("key", "value", "refusal"),
    [
        ("PYRACAST_WEATHER", "1", "PYRACAST_WEATHER is not a setting the application reads."),
        ("PGPASSWORD", "x", "PGPASSWORD is managed by rotate."),
        ("PYRACAST_SECRET_KEY", "x", "PYRACAST_SECRET_KEY is managed by rotate."),
        ("EPIAS_USERNAME", "", "Enter a value of one line."),
        ("EPIAS_USERNAME", "a\nb", "Enter a value of one line."),
    ],
)
def test_set_refuses_an_unknown_or_managed_key_and_an_invalid_value(
    created: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    key: str,
    value: str,
    refusal: str,
) -> None:
    before = read(created, "runtime.json")
    monkeypatch.setattr(getpass, "getpass", lambda _prompt: value)
    capsys.readouterr()

    assert run(created, "set", key) == 2

    assert refusal in printed(capsys)
    assert read(created, "runtime.json") == before


def test_set_and_rotate_need_the_files_create_wrote(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(tmp_path, "rotate", "secret-key") == 2
    assert run(tmp_path, "set", "EPIAS_USERNAME") == 2

    assert printed(capsys).count("runtime.json is missing or not a JSON object.") == 2


def test_check_names_a_missing_runtime_file_without_reading_further(
    created: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (created / "runtime.json").unlink()
    capsys.readouterr()

    assert run(created, "check") == 1

    assert printed(capsys) == "runtime.json is missing or not a JSON object.\n"
