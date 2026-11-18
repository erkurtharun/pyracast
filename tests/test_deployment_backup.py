import base64
import hashlib
import io
import json
import signal
import subprocess
import time
import urllib.request
from collections.abc import Iterator
from email.message import Message
from http.client import HTTPException
from pathlib import Path
from typing import BinaryIO, cast
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from deploy import backup, host, operations_log


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
        return io.BytesIO(
            json.dumps(
                {
                    "md5Hash": md5(contents) if valid_checksum else "invalid",
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
    monkeypatch.setattr(host, "LAST_BACKUP", tmp_path / "last-backup.json")
    monkeypatch.setattr(host, "holds_release_lock", lambda descriptor: descriptor == 9)
    with pytest.raises(subprocess.CalledProcessError):
        backup.create_backup(tmp_path, "private-backups", "prerelease", tmp_path, 9)
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
        return io.BytesIO(json.dumps({"md5Hash": md5(b"PGDMP"), "size": "5"}).encode())

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


def md5(contents: bytes) -> str:
    return base64.b64encode(hashlib.md5(contents, usedforsecurity=False).digest()).decode()


def refusal(request: urllib.request.Request, status: int) -> HTTPError:
    return HTTPError(request.full_url, status, "refused", Message(), None)


def test_upload_asks_cloud_storage_to_validate_the_md5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "database.dump"
    archive.write_bytes(b"PGDMP")
    hashes: list[str | None] = []

    def upload(request: urllib.request.Request, **_kwargs: object) -> io.BytesIO:
        hashes.append(request.get_header("X-goog-hash"))
        return io.BytesIO(json.dumps({"md5Hash": md5(b"PGDMP"), "size": "5"}).encode())

    monkeypatch.setattr(backup, "access_token", lambda: "private-token")
    monkeypatch.setattr(urllib.request, "urlopen", upload)
    backup.upload_backup("private-backups", "postgres/hourly.dump", archive)
    assert hashes == [f"md5={md5(b'PGDMP')}"]


@pytest.mark.parametrize(("status", "attempts"), [(400, 1), (401, 1), (403, 1), (404, 1)])
def test_permanent_upload_refusals_are_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int, attempts: int
) -> None:
    archive = tmp_path / "database.dump"
    archive.write_bytes(b"PGDMP")
    requests: list[str] = []

    def upload(request: urllib.request.Request, **_kwargs: object) -> io.BytesIO:
        requests.append(request.get_method())
        raise refusal(request, status)

    monkeypatch.setattr(backup, "access_token", lambda: "private-token")
    monkeypatch.setattr(urllib.request, "urlopen", upload)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with pytest.raises(HTTPError):
        backup.upload_backup("private-backups", "postgres/hourly.dump", archive)
    assert len(requests) == attempts


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_transient_upload_statuses_are_retried_within_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    archive = tmp_path / "database.dump"
    archive.write_bytes(b"PGDMP")
    requests: list[str] = []

    def upload(request: urllib.request.Request, **_kwargs: object) -> io.BytesIO:
        requests.append(request.get_method())
        raise refusal(request, status)

    monkeypatch.setattr(backup, "access_token", lambda: "private-token")
    monkeypatch.setattr(urllib.request, "urlopen", upload)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with pytest.raises(HTTPError):
        backup.upload_backup("private-backups", "postgres/hourly.dump", archive)
    assert requests == ["POST", "POST", "POST"]


def test_metadata_check_after_a_lost_upload_response_retries_transport_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "database.dump"
    archive.write_bytes(b"PGDMP")
    methods: list[str] = []

    def upload(request: urllib.request.Request, **_kwargs: object) -> io.BytesIO:
        methods.append(request.get_method())
        if request.get_method() == "POST":
            raise refusal(request, 412)
        if methods.count("GET") == 1:
            raise URLError("metadata connection reset")
        return io.BytesIO(json.dumps({"md5Hash": md5(b"PGDMP"), "size": "5"}).encode())

    monkeypatch.setattr(backup, "access_token", lambda: "private-token")
    monkeypatch.setattr(urllib.request, "urlopen", upload)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    backup.upload_backup("private-backups", "postgres/hourly.dump", archive)
    assert methods == ["POST", "GET", "POST", "GET"]


def test_existing_object_with_another_checksum_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "database.dump"
    archive.write_bytes(b"PGDMP")

    def upload(request: urllib.request.Request, **_kwargs: object) -> io.BytesIO:
        if request.get_method() == "POST":
            raise refusal(request, 412)
        return io.BytesIO(json.dumps({"md5Hash": md5(b"other"), "size": "5"}).encode())

    monkeypatch.setattr(backup, "access_token", lambda: "private-token")
    monkeypatch.setattr(urllib.request, "urlopen", upload)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with pytest.raises(ValueError, match="Existing backup checksum does not match"):
        backup.upload_backup("private-backups", "postgres/hourly.dump", archive)


@pytest.mark.parametrize("corrupt", [False, True])
def test_rehearsal_verifies_the_downloaded_archive_before_restoring_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corrupt: bool
) -> None:
    contents = b"PGDMP-archive"
    restored: list[bytes] = []

    def download(request: urllib.request.Request, **_kwargs: object) -> io.BytesIO:
        if "alt=media" in request.full_url:
            return io.BytesIO(contents + (b"-truncated-elsewhere" if corrupt else b""))
        return io.BytesIO(json.dumps({"md5Hash": md5(contents), "size": "13"}).encode())

    def restore(_release: Path, archive: Path, _name: str) -> None:
        restored.append(archive.read_bytes())

    monkeypatch.setattr(backup, "access_token", lambda: "private-token")
    monkeypatch.setattr(urllib.request, "urlopen", download)
    monkeypatch.setattr(backup, "restore_archive", restore)
    if corrupt:
        with pytest.raises(ValueError, match="Downloaded backup does not match"):
            backup.rehearse_restore(tmp_path, "private-backups", "postgres/x.dump", tmp_path)
        assert restored == []
    else:
        backup.rehearse_restore(tmp_path, "private-backups", "postgres/x.dump", tmp_path)
        assert restored == [contents]


@pytest.fixture
def fresh_instance_metadata() -> Iterator[None]:
    operations_log.log_resource.cache_clear()
    yield
    operations_log.log_resource.cache_clear()


@pytest.mark.usefixtures("fresh_instance_metadata")
def test_instance_metadata_is_read_once_per_process_with_a_short_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "project/project-id": "private-project",
        "instance/id": "1",
        "instance/zone": "projects/1/zones/europe-west1-b",
        "instance/service-accounts/default/token": json.dumps({"access_token": "token"}),
    }
    reads: list[tuple[str, object]] = []
    written: list[dict[str, object]] = []

    def urlopen(request: urllib.request.Request, timeout: float) -> io.BytesIO:
        if "metadata.google.internal" in request.full_url:
            path = request.full_url.split("/computeMetadata/v1/", 1)[1]
            reads.append((path, timeout))
            return io.BytesIO(values[path].encode())
        assert request.data is not None
        written.append(json.loads(cast(bytes, request.data)))
        return io.BytesIO(b"{}")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    operations_log.report("pyracast_watchdog_heartbeat", "INFO", state="ok")
    operations_log.report("pyracast_watchdog_heartbeat", "INFO", state="blocked")

    assert [path for path, _ in reads] == [
        "project/project-id",
        "instance/id",
        "instance/zone",
        "instance/service-accounts/default/token",
        "instance/service-accounts/default/token",
    ]
    assert {timeout for _, timeout in reads} == {2}
    assert [entry["resource"] for entry in written] == [
        {
            "type": "gce_instance",
            "labels": {
                "project_id": "private-project",
                "instance_id": "1",
                "zone": "europe-west1-b",
            },
        }
    ] * 2


def forbidden(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("This step must not run.")


@pytest.mark.parametrize("name", ["other/x.dump", "postgres/x.sql", "postgres/a b.dump"])
def test_a_rehearsal_restores_only_a_postgresql_archive_of_the_bucket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setattr(backup, "download_backup", forbidden)
    with pytest.raises(ValueError, match="Select a PostgreSQL archive object"):
        backup.rehearse_restore(tmp_path, "private-backups", name, tmp_path)


def test_a_restore_database_that_never_accepts_connections_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "database.dump"
    archive.write_bytes(b"PGDMP")
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 2 if "pg_isready" in command else 0)

    def configuration(_command: list[str], **_kwargs: object) -> str:
        return json.dumps({"services": {"postgres": {"image": "postgres@sha256:fixture"}}})

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "check_output", configuration)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with pytest.raises(RuntimeError, match="Restore database did not become ready"):
        backup.restore_archive(tmp_path, archive, "postgres/fixture.dump")
    assert sum("pg_isready" in command for command in commands) == 60
    assert not any("pg_restore" in command for command in commands)
    assert [command[:3] for command in commands[-2:]] == [
        ["docker", "rm", "--force"],
        ["docker", "volume", "rm"],
    ]


def host_configuration(tmp_path: Path, bucket: str) -> Path:
    config = tmp_path / "host.json"
    config.write_text(json.dumps({"backup_bucket": bucket}))
    return config


@pytest.mark.parametrize("bucket", ["", "Private", "-backups", "a"])
def test_backup_refuses_a_bucket_name_cloud_storage_would_not_accept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bucket: str
) -> None:
    monkeypatch.setattr(backup, "create_backup", forbidden)
    with pytest.raises(ValueError, match="Invalid backup bucket"):
        backup.main([str(host_configuration(tmp_path, bucket))])


def test_a_rehearsal_restores_the_named_archive_and_takes_no_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rehearsed: list[tuple[Path, str, str]] = []

    def rehearse(release: Path, bucket: str, name: str, _directory: Path) -> None:
        rehearsed.append((release, bucket, name))

    monkeypatch.setattr(backup, "rehearse_restore", rehearse)
    monkeypatch.setattr(backup, "create_backup", forbidden)
    config = host_configuration(tmp_path, "private-backups")
    arguments = [str(config), "--release-dir", str(tmp_path), "--rehearse", "postgres/x.dump"]

    assert backup.main(arguments) == 0
    assert rehearsed == [(tmp_path, "private-backups", "postgres/x.dump")]


def test_a_refused_command_line_is_not_reported_as_a_failed_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reports: list[str] = []
    monkeypatch.setattr(signal, "signal", lambda *_args: None)
    monkeypatch.setattr(backup, "report", lambda event, *_args, **_kwargs: reports.append(event))
    with pytest.raises(SystemExit) as stopped:
        backup.run(["--kind", "weekly", str(tmp_path / "host.json")])
    assert stopped.value.code == 2
    assert reports == []
