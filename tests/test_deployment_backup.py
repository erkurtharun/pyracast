import base64
import hashlib
import io
import json
import re
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from email.message import Message
from http.client import HTTPException
from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO, NamedTuple, cast
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from deploy import backup, host, operations_log
from tests.support.processes import interpreter


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
        raise subprocess.CalledProcessError(3, command)

    def upload(_bucket: str, name: str, _archive: Path) -> None:
        uploaded.append(name)

    monkeypatch.setattr(backup, "bounded_run", failed_dump)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(backup, "upload_backup", upload)
    monkeypatch.setattr(host, "LAST_BACKUP", tmp_path / "last-backup.json")
    monkeypatch.setattr(host, "RECOVERY_STATE", tmp_path / "reset")
    monkeypatch.setattr(host, "holds_release_lock", lambda descriptor: descriptor == 9)
    with pytest.raises(subprocess.CalledProcessError):
        backup.create_backup(tmp_path, "private-backups", "prerelease", tmp_path, 9)
    assert uploaded == []


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


class Process:
    def __init__(self, waits: list[int | None]) -> None:
        self.waits = waits
        self.killed = False

    def __enter__(self) -> Process:
        return self

    def __exit__(self, *_exception: object) -> None:
        return None

    def wait(self, timeout: float) -> int:
        assert timeout == 1
        status = self.waits.pop(0)
        if status is None:
            raise subprocess.TimeoutExpired(["fixture"], timeout)
        return status

    def kill(self) -> None:
        self.killed = True


class Disk(NamedTuple):
    total: int
    used: int
    free: int


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [0.0]
    monkeypatch.setattr(backup, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


def bounded(
    monkeypatch: pytest.MonkeyPatch, process: Process, free: list[int], clock: list[float]
) -> list[list[str]]:
    started: list[list[str]] = []

    def popen(command: list[str], **_options: object) -> Process:
        started.append(command)
        return process

    def usage(_path: Path) -> Disk:
        clock[0] += 10
        return Disk(100, 100 - free[0], free.pop(0) if len(free) > 1 else free[0])

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(shutil, "disk_usage", usage)
    return started


def test_a_bounded_command_that_finishes_in_time_and_space_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    process = Process([None, None, 0])
    started = bounded(monkeypatch, process, [50], clock)

    backup.bounded_run(
        ["pg_dump"], stdin=None, stdout=None, stderr=None, timeout=600, disk=tmp_path
    )

    assert started == [["pg_dump"]]
    assert not process.killed


def test_a_bounded_command_is_killed_when_its_disk_falls_below_the_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    process = Process([None, None])
    bounded(monkeypatch, process, [50, 50, 4], clock)

    with pytest.raises(backup.CapacityError):
        backup.bounded_run(
            ["pg_dump"], stdin=None, stdout=None, stderr=None, timeout=600, disk=tmp_path
        )

    assert process.killed


def test_a_bounded_command_does_not_start_on_a_disk_already_below_the_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    process = Process([0])
    started = bounded(monkeypatch, process, [4], clock)

    with pytest.raises(backup.CapacityError):
        backup.bounded_run(
            ["pg_dump"], stdin=None, stdout=None, stderr=None, timeout=600, disk=tmp_path
        )

    assert started == []


def test_a_bounded_command_is_killed_at_its_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    process = Process([None] * 10)
    bounded(monkeypatch, process, [50], clock)

    with pytest.raises(subprocess.TimeoutExpired):
        backup.bounded_run(
            ["pg_restore"], stdin=None, stdout=None, stderr=None, timeout=25, disk=tmp_path
        )

    assert process.killed
    assert clock[0] >= 25


def test_a_bounded_command_that_fails_raises_its_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    bounded(monkeypatch, Process([3]), [50], clock)

    with pytest.raises(subprocess.CalledProcessError) as failed:
        backup.bounded_run(
            ["psql"], stdin=None, stdout=None, stderr=None, timeout=600, disk=tmp_path
        )

    assert failed.value.returncode == 3


SLEEPER = "import sys, time; sys.stdout.buffer.write(b'PGDMP'); sys.stdout.flush(); time.sleep(20)"


def stopped_by_systemd() -> None:
    backup.terminate(signal.SIGTERM, None)


def failing_disk() -> None:
    raise OSError("the disk cannot be read")


def interrupted(
    monkeypatch: pytest.MonkeyPatch, interruption: Callable[[], None]
) -> list[subprocess.Popen[bytes]]:
    started: list[subprocess.Popen[bytes]] = []
    calls = [0]
    usage = shutil.disk_usage

    class Recorded(subprocess.Popen[bytes]):
        def __init__(self, *arguments: Any, **options: Any) -> None:
            super().__init__(*arguments, **options)
            started.append(self)

    def checked(path: Path) -> object:
        calls[0] += 1
        if calls[0] == 2:
            interruption()
        return usage(path)

    monkeypatch.setattr(subprocess, "Popen", Recorded)
    monkeypatch.setattr(shutil, "disk_usage", checked)
    return started


@pytest.mark.parametrize(
    ("interruption", "raised"), [(stopped_by_systemd, SystemExit), (failing_disk, OSError)]
)
def test_a_bounded_command_left_early_is_killed_and_reaped_at_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: Callable[[], None],
    raised: type[BaseException],
) -> None:
    started = interrupted(monkeypatch, interruption)
    began = time.monotonic()

    with pytest.raises(raised):
        backup.bounded_run(
            [interpreter(), "-c", SLEEPER],
            stdin=None,
            stdout=subprocess.DEVNULL,
            stderr=None,
            timeout=600,
            disk=tmp_path,
        )

    assert time.monotonic() - began < 10
    (child,) = started
    assert child.returncode is not None


@pytest.mark.parametrize(
    ("kind", "interruption", "exit_code"),
    [
        ("prerelease", stopped_by_systemd, 143),
        ("hourly", stopped_by_systemd, 143),
        ("prerelease", failing_disk, 1),
        ("hourly", failing_disk, 1),
    ],
)
def test_a_dump_left_early_removes_its_files_ends_its_backends_and_reports_the_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    interruption: Callable[[], None],
    exit_code: int,
) -> None:
    started = interrupted(monkeypatch, interruption)
    terminated: list[list[str]] = []
    reports: list[tuple[str, str, dict[str, object]]] = []
    temporary = tmp_path / "tmp"
    temporary.mkdir()
    recovery = tmp_path / "reset"
    recovery.mkdir()

    @contextmanager
    def release_lock(_wait: float) -> Iterator[bool]:
        yield True

    def run(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        terminated.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(backup, "compose_command", lambda _release: [interpreter(), "-c", SLEEPER])
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(signal, "signal", lambda *_args: None)
    monkeypatch.setattr(tempfile, "tempdir", str(temporary))
    monkeypatch.setattr(host, "release_lock", release_lock)
    monkeypatch.setattr(host, "holds_release_lock", lambda descriptor: descriptor == 9)
    monkeypatch.setattr(host, "RELEASE_BLOCKED", tmp_path / "release-blocked")
    monkeypatch.setattr(host, "RECOVERY_STATE", recovery)
    monkeypatch.setattr(host, "LAST_BACKUP", tmp_path / "last-backup.json")
    monkeypatch.setattr(backup, "upload_backup", forbidden)
    monkeypatch.setattr(
        backup,
        "report",
        lambda event, severity, **details: reports.append((event, severity, details)),
    )
    arguments = [str(host_configuration(tmp_path, "private-backups")), "--kind", kind]
    began = time.monotonic()

    if exit_code == 143:
        with pytest.raises(SystemExit) as stopped:
            backup.run([*arguments, "--lock-fd", "9"])
        assert stopped.value.code == 143
    else:
        assert backup.run([*arguments, "--lock-fd", "9"]) == 1

    assert time.monotonic() - began < 10
    assert started[0].returncode is not None
    assert list(temporary.iterdir()) == []
    assert list(recovery.iterdir()) == []
    assert not (tmp_path / "last-backup.json").exists()
    ((command,),) = [terminated]
    assert "pg_terminate_backend(pid)" in command[-1]
    assert "application_name = 'pyracast-backup'" in command[-1]
    expected = {"reason": "terminated"} if exit_code == 143 else {}
    assert reports == [("pyracast_backup_failed", "ERROR", expected)]


@pytest.mark.parametrize("unwritable", [host.PRERELEASE_RECORD, "last-backup.json"])
def test_a_pre_release_dump_whose_records_cannot_be_written_is_not_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unwritable: str
) -> None:
    write = backup.write_atomically

    def dump(_release: Path, archive: Path) -> dict[str, int]:
        archive.write_bytes(b"PGDMP")
        return {"django_migrations": 40}

    def written(path: Path, content: str) -> None:
        if path.name == unwritable:
            raise OSError("No space left on device")
        write(path, content)

    monkeypatch.setattr(backup, "dump_database", dump)
    monkeypatch.setattr(backup, "upload_backup", lambda _bucket, _name, _archive: None)
    monkeypatch.setattr(backup, "write_atomically", written)
    monkeypatch.setattr(host, "LAST_BACKUP", tmp_path / "last-backup.json")
    monkeypatch.setattr(host, "clock_mark", lambda: {"boot_id": "b", "uptime_seconds": 1.0})
    recovery = tmp_path / "reset"
    recovery.mkdir()

    with pytest.raises(OSError, match="No space"):
        backup.store_backup(tmp_path, "private-backups", "prerelease", recovery)

    assert list(recovery.iterdir()) == []


def test_the_dump_counts_every_table_in_the_snapshot_it_exports_to_pg_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[list[str], bytes, Path]] = []

    def run(
        command: list[str],
        *,
        stdin: BinaryIO,
        stdout: BinaryIO,
        stderr: BinaryIO,
        timeout: float,
        disk: Path,
    ) -> None:
        calls.append((command, stdin.read(), disk))
        assert timeout == backup.DUMP_TIMEOUT_SECONDS
        stdout.write(b"PGDMP")
        stderr.write(b'pyracast-counts { "plants_plant" : 3, "django_migrations" : 40 }\n')
        stderr.write(b"pg_dump: warning: fixture\n")

    monkeypatch.setattr(backup, "bounded_run", run)
    monkeypatch.setattr(subprocess, "run", forbidden)
    archive = tmp_path / "database.dump"

    counts = backup.dump_database(tmp_path, archive)

    ((command, script, disk),) = calls
    assert counts == {"plants_plant": 3, "django_migrations": 40}
    assert archive.read_bytes() == b"PGDMP"
    assert disk == tmp_path
    assert command[command.index("exec") :] == [
        "exec",
        "-T",
        "--env",
        "PGAPPNAME=pyracast-backup",
        "postgres",
        "psql",
        "-X",
        "-q",
        "-At",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        "postgres",
        "-d",
        "pyracast",
    ]
    lines = script.decode().splitlines()
    assert lines[0] == "BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY;"
    assert lines[1] == "SELECT pg_export_snapshot() AS snapshot \\gset"
    assert lines.index("\\setenv PYRACAST_SNAPSHOT :snapshot") < next(
        index for index, line in enumerate(lines) if line.startswith("\\! pg_dump ")
    )
    assert '--snapshot="$PYRACAST_SNAPSHOT"' in script.decode()
    assert lines[-1] == "COMMIT;"
    assert capsys.readouterr().err == "pg_dump: warning: fixture\n"


@pytest.mark.parametrize("errors", ["", "pyracast-counts not json\n", "pyracast-counts []\n"])
def test_a_dump_that_reports_no_table_counts_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, errors: str
) -> None:
    def run(command: list[str], *, stderr: BinaryIO, **_options: object) -> None:
        stderr.write(errors.encode())

    monkeypatch.setattr(backup, "bounded_run", run)
    monkeypatch.setattr(subprocess, "run", forbidden)

    with pytest.raises(ValueError, match="no table counts"):
        backup.dump_database(tmp_path, tmp_path / "database.dump")


def rewrite(
    responses: list[dict[str, object] | int],
) -> tuple[list[urllib.request.Request], Callable[..., io.BytesIO]]:
    requests: list[urllib.request.Request] = []

    def urlopen(request: urllib.request.Request, **_kwargs: object) -> io.BytesIO:
        requests.append(request)
        response = responses.pop(0)
        if isinstance(response, int):
            raise refusal(request, response)
        return io.BytesIO(json.dumps(response).encode())

    return requests, urlopen


def test_the_daily_copy_rewrites_the_backup_once_a_day_under_its_own_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests, urlopen = rewrite([{"done": False, "rewriteToken": "next"}, {"done": True}])
    monkeypatch.setattr(backup, "access_token", lambda: "private-token")
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    copied = backup.copy_daily("private-backups", "postgres/2026/11/20/x-hourly-y.dump")

    assert copied is not None
    assert re.fullmatch(r"daily/\d{4}/\d{2}/\d{2}\.dump", copied)
    first, second = (urlsplit(request.full_url) for request in requests)
    assert first.path == (
        "/storage/v1/b/private-backups/o/postgres%2F2026%2F11%2F20%2Fx-hourly-y.dump"
        f"/rewriteTo/b/private-backups/o/{copied.replace('/', '%2F')}"
    )
    assert parse_qs(first.query) == {"ifGenerationMatch": ["0"]}
    assert parse_qs(second.query) == {"ifGenerationMatch": ["0"], "rewriteToken": ["next"]}
    assert {request.get_method() for request in requests} == {"POST"}


def test_a_daily_copy_that_exists_already_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    requests, urlopen = rewrite([412])
    monkeypatch.setattr(backup, "access_token", lambda: "private-token")
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    assert backup.copy_daily("private-backups", "postgres/x.dump") is None
    assert len(requests) == 1


@pytest.mark.parametrize("responses", [[403], [{"done": False, "rewriteToken": "t"}] * 10])
def test_a_daily_copy_that_cannot_finish_fails(
    monkeypatch: pytest.MonkeyPatch, responses: list[dict[str, object] | int]
) -> None:
    _requests, urlopen = rewrite(list(responses))
    monkeypatch.setattr(backup, "access_token", lambda: "private-token")
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    with pytest.raises((HTTPError, RuntimeError)):
        backup.copy_daily("private-backups", "postgres/x.dump")


@pytest.mark.parametrize(
    ("enabled", "outcome", "events"),
    [
        (False, None, ["pyracast_backup_succeeded"]),
        (
            True,
            "daily/2026/11/20.dump",
            ["pyracast_backup_succeeded", "pyracast_backup_daily_copied"],
        ),
        (True, None, ["pyracast_backup_succeeded"]),
        (
            True,
            OSError("private detail"),
            ["pyracast_backup_succeeded", "pyracast_backup_daily_copy_failed"],
        ),
    ],
)
def test_the_hourly_backup_keeps_a_daily_copy_only_where_the_host_enables_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    outcome: str | Exception | None,
    events: list[str],
) -> None:
    reports: list[tuple[str, str, dict[str, object]]] = []
    copies: list[str] = []

    @contextmanager
    def release_lock(_wait: float) -> Iterator[bool]:
        yield True

    def store(_release: Path, _bucket: str, _kind: str, directory: Path) -> tuple[str, Path]:
        archive = directory / "x.dump"
        archive.write_bytes(b"PGDMP")
        return "postgres/x-hourly-y.dump", archive

    def copy(_bucket: str, name: str) -> str | None:
        copies.append(name)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(host, "release_lock", release_lock)
    monkeypatch.setattr(host, "RELEASE_BLOCKED", tmp_path / "release-blocked")
    monkeypatch.setattr(backup, "store_backup", store)
    monkeypatch.setattr(backup, "copy_daily", copy)
    monkeypatch.setattr(
        backup,
        "report",
        lambda event, severity, **details: reports.append((event, severity, details)),
    )
    config = tmp_path / "host.json"
    config.write_text(
        json.dumps(
            {"backup_bucket": "private-backups", **({"daily_backups": True} if enabled else {})}
        )
    )

    assert backup.main([str(config), "--release-dir", str(tmp_path)]) == 0
    assert [event for event, _, _ in reports] == events
    assert copies == (["postgres/x-hourly-y.dump"] if enabled else [])
    if isinstance(outcome, Exception):
        assert reports[-1][1:] == ("WARNING", {"error": "OSError"})


@pytest.mark.parametrize("blocked", [False, True])
@pytest.mark.parametrize("value", ["true", 1, None, [True]])
def test_an_ill_typed_daily_setting_fails_an_hourly_run_and_says_whether_it_kept_a_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: object,
    blocked: bool,
) -> None:
    reports: list[tuple[str, str, dict[str, object]]] = []
    stored: list[str] = []

    @contextmanager
    def release_lock(_wait: float) -> Iterator[bool]:
        yield True

    def store(_release: Path, _bucket: str, kind: str, directory: Path) -> tuple[str, Path]:
        stored.append(kind)
        archive = directory / "x.dump"
        archive.write_bytes(b"PGDMP")
        return "postgres/x-hourly-y.dump", archive

    def copy(_bucket: str, _name: str) -> str:
        pytest.fail("an ill-typed setting makes no daily copy")

    monkeypatch.setattr(host, "release_lock", release_lock)
    monkeypatch.setattr(host, "RELEASE_BLOCKED", tmp_path / "release-blocked")
    if blocked:
        (tmp_path / "release-blocked").touch()
    monkeypatch.setattr(backup, "store_backup", store)
    monkeypatch.setattr(backup, "copy_daily", copy)
    monkeypatch.setattr(
        backup,
        "report",
        lambda event, severity, **details: reports.append((event, severity, details)),
    )
    config = tmp_path / "host.json"
    config.write_text(json.dumps({"backup_bucket": "private-backups", "daily_backups": value}))

    assert backup.main([str(config), "--release-dir", str(tmp_path)]) == 1
    assert stored == ([] if blocked else ["hourly"])
    assert [event for event, _, _ in reports] == [
        "pyracast_backup_skipped" if blocked else "pyracast_backup_succeeded",
        "pyracast_backup_setting_invalid",
    ]
    assert reports[-1][1:] == ("ERROR", {"setting": "daily_backups"})
    outcome = (
        "this run took no backup, and no daily copy is made until it is fixed"
        if blocked
        else "the hourly backup was kept, but no daily copy was made"
    )
    assert capsys.readouterr().err.splitlines() == [
        f"The daily_backups setting of {config} is neither true nor false; {outcome}."
    ]


@pytest.mark.parametrize("value", ["true", 1, None, [True]])
def test_an_ill_typed_daily_setting_never_fails_a_prerelease_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: object,
) -> None:
    reports: list[tuple[str, str, dict[str, object]]] = []
    stored: list[str] = []

    def store(_release: Path, _bucket: str, kind: str, directory: Path) -> tuple[str, Path]:
        stored.append(kind)
        archive = directory / "x.dump"
        archive.write_bytes(b"PGDMP")
        return "postgres/x-prerelease-y.dump", archive

    def copy(_bucket: str, _name: str) -> str:
        pytest.fail("a pre-release backup makes no daily copy")

    monkeypatch.setattr(host, "holds_release_lock", lambda descriptor: descriptor == 9)
    monkeypatch.setattr(host, "RECOVERY_STATE", tmp_path / "reset")
    monkeypatch.setattr(backup, "store_backup", store)
    monkeypatch.setattr(backup, "copy_daily", copy)
    monkeypatch.setattr(
        backup,
        "report",
        lambda event, severity, **details: reports.append((event, severity, details)),
    )
    config = tmp_path / "host.json"
    config.write_text(json.dumps({"backup_bucket": "private-backups", "daily_backups": value}))

    assert (
        backup.main(
            [str(config), "--release-dir", str(tmp_path), "--kind", "prerelease", "--lock-fd", "9"]
        )
        == 0
    )
    assert stored == ["prerelease"]
    assert [(event, severity) for event, severity, _ in reports] == [
        ("pyracast_backup_succeeded", "INFO"),
        ("pyracast_backup_setting_invalid", "ERROR"),
    ]
    assert capsys.readouterr().err.splitlines() == [
        f"WARNING: the daily_backups setting of {config} is neither true nor false; "
        "the pre-release backup does not use it."
    ]


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
