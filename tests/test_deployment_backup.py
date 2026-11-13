import base64
import hashlib
import io
import json
import subprocess
import time
import urllib.request
from email.message import Message
from http.client import HTTPException
from pathlib import Path
from typing import BinaryIO, cast
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from deploy import backup, operations_log


@pytest.mark.parametrize("valid_checksum", [True, False])
def test_backup_upload_verifies_content_and_prevents_overwriting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    valid_checksum: bool,
) -> None:
    archive = tmp_path / "database.dump"
    contents = b"PGDMP-custom-archive"
    archive.write_bytes(contents)
    requests: list[urllib.request.Request] = []

    def upload(request: urllib.request.Request, **_kwargs: object) -> io.BytesIO:
        requests.append(request)
        assert request.data is not None
        assert cast(BinaryIO, request.data).read() == contents
        checksum = base64.b64encode(hashlib.md5(contents).digest()).decode()  # noqa: S324
        return io.BytesIO(
            json.dumps(
                {
                    "md5Hash": checksum if valid_checksum else "invalid",
                    "size": str(len(contents)),
                }
            ).encode()
        )

    monkeypatch.setattr(backup, "access_token", lambda: "private-token")
    monkeypatch.setattr(urllib.request, "urlopen", upload)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    if valid_checksum:
        backup.upload_backup("private-backups", "postgres/hourly.dump", archive)
        assert len(requests) == 1
    else:
        with pytest.raises(ValueError, match="checksum"):
            backup.upload_backup("private-backups", "postgres/hourly.dump", archive)
        assert len(requests) == 3
    query = parse_qs(urlsplit(requests[0].full_url).query)
    assert query == {
        "uploadType": ["media"],
        "name": ["postgres/hourly.dump"],
        "ifGenerationMatch": ["0"],
    }
    assert requests[0].get_header("Authorization") == "Bearer private-token"


def test_failed_dump_cannot_publish_a_successful_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uploaded: list[str] = []

    def failed_dump(command: list[str], **_kwargs: object) -> None:
        raise subprocess.CalledProcessError(1, command)

    def upload(_bucket: str, name: str, _archive: Path) -> None:
        uploaded.append(name)

    monkeypatch.setattr(subprocess, "run", failed_dump)
    monkeypatch.setattr(backup, "upload_backup", upload)
    with pytest.raises(subprocess.CalledProcessError):
        backup.create_backup(tmp_path, "private-backups", "hourly", tmp_path)
    assert uploaded == []


def test_restore_failure_removes_isolated_database_and_volume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "database.dump"
    archive.write_bytes(b"invalid dump")
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        if "pg_restore" in command:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    def configuration(_command: list[str], **_kwargs: object) -> str:
        return json.dumps({"services": {"postgres": {"image": "postgres@sha256:fixture"}}})

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "check_output", configuration)
    with pytest.raises(subprocess.CalledProcessError):
        backup.restore_archive(tmp_path, archive, "postgres/fixture.dump")
    assert commands[-2][:3] == ["docker", "rm", "--force"]
    assert commands[-1][:3] == ["docker", "volume", "rm"]
    start = next(command for command in commands if command[:2] == ["docker", "run"])
    assert start[start.index("--network") + 1] == "none"
    assert not any("5432:" in argument for argument in start)


def test_backup_recovers_when_upload_committed_before_response_was_lost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "database.dump"
    archive.write_bytes(b"PGDMP")
    methods: list[str] = []

    def upload(request: urllib.request.Request, **_kwargs: object) -> io.BytesIO:
        methods.append(request.get_method())
        if request.get_method() == "POST":
            raise HTTPError(request.full_url, 412, "Already exists", Message(), None)
        checksum = base64.b64encode(hashlib.md5(b"PGDMP").digest()).decode()  # noqa: S324
        return io.BytesIO(json.dumps({"md5Hash": checksum, "size": "5"}).encode())

    monkeypatch.setattr(backup, "access_token", lambda: "private-token")
    monkeypatch.setattr(urllib.request, "urlopen", upload)
    backup.upload_backup("private-backups", "postgres/hourly.dump", archive)
    assert methods == ["POST", "GET"]


def test_restore_waits_for_the_final_tcp_server_instead_of_the_init_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "database.dump"
    archive.write_bytes(b"PGDMP")
    tcp_probes = 0
    restored = False

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal tcp_probes, restored
        if "pg_isready" in command:
            if "-h" not in command:
                return subprocess.CompletedProcess(command, 0)
            assert command[command.index("-h") + 1] == "127.0.0.1"
            tcp_probes += 1
            return subprocess.CompletedProcess(command, 2 if tcp_probes == 1 else 0)
        if "pg_restore" in command:
            assert tcp_probes == 2, "The temporary initdb server must not accept a restore."
            assert command[command.index("-h") + 1] == "127.0.0.1"
            restored = True
        return subprocess.CompletedProcess(command, 0)

    def configuration(_command: list[str], **_kwargs: object) -> str:
        return json.dumps({"services": {"postgres": {"image": "postgres@sha256:fixture"}}})

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "check_output", configuration)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(backup, "report", lambda *_args, **_kwargs: None)
    backup.restore_archive(tmp_path, archive, "postgres/fixture.dump")
    assert restored


@pytest.mark.parametrize("failure", [RuntimeError, TypeError])
def test_backup_does_not_retry_programming_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[Exception],
) -> None:
    archive = tmp_path / "database.dump"
    archive.write_bytes(b"PGDMP")
    attempts = 0

    def upload(_request: urllib.request.Request, **_kwargs: object) -> io.BytesIO:
        nonlocal attempts
        attempts += 1
        raise failure("internal failure")

    monkeypatch.setattr(backup, "access_token", lambda: "private-token")
    monkeypatch.setattr(urllib.request, "urlopen", upload)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with pytest.raises(failure, match="internal failure"):
        backup.upload_backup("private-backups", "postgres/hourly.dump", archive)
    assert attempts == 1


@pytest.mark.parametrize("failure", [RuntimeError, TypeError])
def test_operations_logging_does_not_hide_programming_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: type[Exception],
) -> None:
    def metadata(_path: str) -> str:
        raise failure("private-error-detail")

    monkeypatch.setattr(operations_log, "metadata", metadata)
    with pytest.raises(failure, match="private-error-detail"):
        operations_log.report("pyracast_backup_failed", "ERROR")
    assert capsys.readouterr().err.splitlines() == ['{"event": "pyracast_backup_failed"}']


@pytest.mark.parametrize("failure", [OSError, ValueError, HTTPException])
def test_operations_logging_preserves_redacted_fallback_for_expected_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: type[Exception],
) -> None:
    def metadata(_path: str) -> str:
        raise failure("private-error-detail")

    monkeypatch.setattr(operations_log, "metadata", metadata)
    operations_log.report("pyracast_backup_failed", "ERROR")
    assert [json.loads(line) for line in capsys.readouterr().err.splitlines()] == [
        {"event": "pyracast_backup_failed"},
        {"event": "pyracast_operations_log_failed"},
    ]


@pytest.mark.parametrize(
    "payload", [[], {}, {"access_token": None}, {"access_token": 123}, {"access_token": ""}]
)
def test_invalid_metadata_credentials_have_a_redacted_data_error(
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
) -> None:
    monkeypatch.setattr(operations_log, "metadata", lambda _path: json.dumps(payload))
    with pytest.raises(ValueError, match="Invalid instance access token response"):
        operations_log.access_token()


@pytest.mark.parametrize("failure", [OSError, HTTPException])
def test_backup_retries_expected_transport_failures_with_a_bounded_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[Exception],
) -> None:
    archive = tmp_path / "database.dump"
    archive.write_bytes(b"PGDMP")
    attempts = 0

    def upload(_request: urllib.request.Request, **_kwargs: object) -> io.BytesIO:
        nonlocal attempts
        attempts += 1
        raise failure("private-error-detail")

    monkeypatch.setattr(backup, "access_token", lambda: "private-token")
    monkeypatch.setattr(urllib.request, "urlopen", upload)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with pytest.raises(failure):
        backup.upload_backup("private-backups", "postgres/hourly.dump", archive)
    assert attempts == 3


@pytest.mark.parametrize("payload", [[], {}, {"md5Hash": "invalid", "size": None}])
def test_malformed_backup_confirmation_has_a_bounded_redacted_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
) -> None:
    archive = tmp_path / "database.dump"
    archive.write_bytes(b"PGDMP")
    monkeypatch.setattr(backup, "access_token", lambda: "private-token")
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda _request, **_kwargs: io.BytesIO(json.dumps(payload).encode()),
    )
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with pytest.raises(ValueError, match="Uploaded backup checksum does not match"):
        backup.upload_backup("private-backups", "postgres/hourly.dump", archive)
