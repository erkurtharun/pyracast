import json
import re
import signal
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from deploy import backup, host


class BackupHost:
    def __init__(self, root: Path) -> None:
        self.release = root / "release"
        self.release.mkdir()
        self.directory = root / "work"
        self.directory.mkdir()
        self.blocked = root / "release-blocked"
        self.last_backup = root / "last-backup.json"
        self.granted = True
        self.held = False
        self.on_lock: Callable[[], None] = lambda: None
        self.waits: list[float] = []
        self.dumps: list[tuple[list[str], bool]] = []
        self.commands: list[list[str]] = []
        self.uploads: list[tuple[str, bool]] = []
        self.dump_error: BaseException | None = None
        self.events: list[tuple[str, str, dict[str, object]]] = []
        self.handlers: dict[int, Callable[[int, object], None]] = {}
        self.pointer_held = False
        self.on_upload: Callable[[str], None] = lambda _name: None

    @contextmanager
    def release_lock(self, wait_seconds: float) -> Iterator[bool]:
        self.waits.append(wait_seconds)
        if not self.granted:
            yield False
            return
        self.held = True
        self.on_lock()
        try:
            yield True
        finally:
            self.held = False

    @contextmanager
    def pointer_lock(self) -> Iterator[None]:
        assert not self.pointer_held
        self.pointer_held = True
        try:
            yield
        finally:
            self.pointer_held = False

    def run(self, command: list[str], **options: Any) -> subprocess.CompletedProcess[bytes]:
        self.commands.append(command)
        if "pg_dump" in command:
            self.dumps.append((command, self.held))
            if self.dump_error is not None:
                raise self.dump_error
            stream: BinaryIO = options["stdout"]
            stream.write(b"PGDMP")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    def upload(self, _bucket: str, name: str, archive: Path) -> None:
        assert archive.read_bytes() == b"PGDMP"
        self.uploads.append((name, self.held))
        self.on_upload(name)

    def report(self, event: str, severity: str, **details: object) -> None:
        self.events.append((event, severity, details))


@pytest.fixture
def backup_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BackupHost:
    fake = BackupHost(tmp_path)
    monkeypatch.setattr(host, "RELEASE_BLOCKED", fake.blocked)
    monkeypatch.setattr(host, "LAST_BACKUP", fake.last_backup)
    monkeypatch.setattr(host, "release_lock", fake.release_lock)
    monkeypatch.setattr(host, "pointer_lock", fake.pointer_lock, raising=False)
    monkeypatch.setattr(backup, "upload_backup", fake.upload)
    monkeypatch.setattr(backup, "report", fake.report)
    monkeypatch.setattr(subprocess, "run", fake.run)
    monkeypatch.setattr(signal, "signal", fake.handlers.__setitem__)
    return fake


def test_hourly_backup_dumps_under_the_release_lock_and_uploads_after_releasing_it(
    backup_host: BackupHost,
) -> None:
    started = time.time()
    name = backup.create_backup(
        backup_host.release, "private-backups", "hourly", backup_host.directory
    )

    assert backup_host.waits == [backup.HOURLY_LOCK_WAIT_SECONDS]
    assert [held for _, held in backup_host.dumps] == [True]
    assert backup_host.uploads == [(name, False)]
    assert name is not None
    assert re.fullmatch(r"postgres/\d{4}/\d{2}/\d{2}/\d{8}T\d{6}Z-hourly-[0-9a-f]{32}\.dump", name)
    marker = json.loads(backup_host.last_backup.read_text())
    assert marker["object"] == name
    assert started <= marker["completed_at"] <= time.time()
    ((event, severity, details),) = backup_host.events
    assert (event, severity) == ("pyracast_backup_succeeded", "INFO")
    assert details["object"] == name
    assert details["size"] == 5
    assert isinstance(details["duration_seconds"], float)


def test_hourly_backup_reports_a_skip_when_the_lock_stays_busy(
    backup_host: BackupHost,
) -> None:
    backup_host.granted = False

    assert (
        backup.create_backup(
            backup_host.release, "private-backups", "hourly", backup_host.directory
        )
        is None
    )
    assert backup_host.dumps == []
    assert backup_host.uploads == []
    assert not backup_host.last_backup.exists()
    assert backup_host.events == [
        ("pyracast_backup_skipped", "WARNING", {"reason": "lock_timeout"})
    ]


def test_hourly_backup_skips_a_release_that_began_while_it_waited(
    backup_host: BackupHost,
) -> None:
    backup_host.on_lock = backup_host.blocked.touch

    assert (
        backup.create_backup(
            backup_host.release, "private-backups", "hourly", backup_host.directory
        )
        is None
    )
    assert backup_host.dumps == []
    assert backup_host.events == [
        ("pyracast_backup_skipped", "WARNING", {"reason": "release_blocked"})
    ]


def test_hourly_backup_started_during_a_release_dumps_once_the_release_finishes(
    backup_host: BackupHost,
) -> None:
    backup_host.blocked.touch()
    backup_host.on_lock = backup_host.blocked.unlink

    name = backup.create_backup(
        backup_host.release, "private-backups", "hourly", backup_host.directory
    )

    assert name is not None
    assert [held for _, held in backup_host.dumps] == [True]
    assert [event for event, _, _ in backup_host.events] == ["pyracast_backup_succeeded"]


def test_hourly_upload_finishing_after_a_newer_prerelease_backup_leaves_the_pointer_on_it(
    backup_host: BackupHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    second = backup_host.directory / "prerelease"
    second.mkdir()
    prerelease: list[str | None] = []
    replace = Path.replace
    unguarded: list[Path] = []

    def release_during_the_hourly_upload(name: str) -> None:
        if "-hourly-" in name:
            prerelease.append(
                backup.create_backup(backup_host.release, "private-backups", "prerelease", second)
            )

    def guarded(source: Path, target: Path) -> Path:
        if target == backup_host.last_backup and not backup_host.pointer_held:
            unguarded.append(target)
        return replace(source, target)

    backup_host.on_upload = release_during_the_hourly_upload
    monkeypatch.setattr(Path, "replace", guarded)
    hourly = backup.create_backup(
        backup_host.release, "private-backups", "hourly", backup_host.directory
    )

    assert hourly is not None
    assert prerelease[0] is not None
    assert [name for name, _ in backup_host.uploads] == [hourly, prerelease[0]]
    assert [details["object"] for _, _, details in backup_host.events] == [prerelease[0], hourly]
    assert json.loads(backup_host.last_backup.read_text())["object"] == prerelease[0]
    assert [path.name for path in backup_host.last_backup.parent.iterdir() if path.is_file()] == [
        backup_host.last_backup.name
    ]
    assert unguarded == []


def test_a_newer_hourly_backup_moves_the_pointer_forward(backup_host: BackupHost) -> None:
    first = backup.create_backup(
        backup_host.release, "private-backups", "prerelease", backup_host.directory
    )
    second = backup.create_backup(
        backup_host.release, "private-backups", "hourly", backup_host.directory
    )

    assert first is not None
    assert second is not None
    pointer = json.loads(backup_host.last_backup.read_text())
    assert pointer["object"] == second
    assert pointer["snapshot_at"] <= pointer["completed_at"]


def test_prerelease_backup_records_its_object_in_the_release_directory(
    backup_host: BackupHost,
) -> None:
    name = backup.create_backup(
        backup_host.release, "private-backups", "prerelease", backup_host.directory
    )

    record = backup_host.release / backup.PRERELEASE_RECORD
    assert json.loads(record.read_text()) == {"object": name}
    assert [path.name for path in backup_host.release.iterdir()] == [record.name]


def test_hourly_backup_writes_no_release_record(backup_host: BackupHost) -> None:
    backup.create_backup(backup_host.release, "private-backups", "hourly", backup_host.directory)

    assert list(backup_host.release.iterdir()) == []


def test_prerelease_backup_never_waits_for_the_lock_its_installer_holds(
    backup_host: BackupHost,
) -> None:
    name = backup.create_backup(
        backup_host.release, "private-backups", "prerelease", backup_host.directory
    )

    assert backup_host.waits == []
    assert len(backup_host.dumps) == 1
    assert name is not None
    assert "-prerelease-" in name


def test_dump_names_its_session_and_ends_it_when_the_dump_times_out(
    backup_host: BackupHost,
) -> None:
    backup_host.dump_error = subprocess.TimeoutExpired(["pg_dump"], backup.DUMP_TIMEOUT_SECONDS)

    with pytest.raises(subprocess.TimeoutExpired):
        backup.dump_database(backup_host.release, backup_host.directory / "database.dump")

    dump, terminate = backup_host.commands
    assert dump[dump.index("exec") : dump.index("postgres")] == [
        "exec",
        "-T",
        "--env",
        "PGAPPNAME=pyracast-backup",
    ]
    assert terminate[terminate.index("exec") + 2] == "postgres"
    assert "psql" in terminate
    assert any(
        "pg_terminate_backend" in argument and "'pyracast-backup'" in argument
        for argument in terminate
    )


def test_skipped_hourly_backup_exits_with_a_temporary_failure(
    backup_host: BackupHost, tmp_path: Path
) -> None:
    backup_host.granted = False
    configuration = tmp_path / "host.json"
    configuration.write_text(json.dumps({"backup_bucket": "private-backups"}))

    assert backup.run([str(configuration), "--release-dir", str(backup_host.release)]) == 75
    assert [event for event, _, _ in backup_host.events] == ["pyracast_backup_skipped"]


def test_successful_backup_exits_cleanly(backup_host: BackupHost, tmp_path: Path) -> None:
    configuration = tmp_path / "host.json"
    configuration.write_text(json.dumps({"backup_bucket": "private-backups"}))

    assert backup.run([str(configuration), "--release-dir", str(backup_host.release)]) == 0
    assert [event for event, _, _ in backup_host.events] == ["pyracast_backup_succeeded"]


def test_terminated_backup_reports_its_failure(
    backup_host: BackupHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(_arguments: list[str] | None) -> int:
        backup_host.handlers[signal.SIGTERM](signal.SIGTERM, None)
        return 0

    monkeypatch.setattr(backup, "main", interrupted)
    with pytest.raises(SystemExit) as stopped:
        backup.run([])
    assert stopped.value.code == 143
    assert backup_host.events == [("pyracast_backup_failed", "ERROR", {"reason": "terminated"})]


def test_unexpected_backup_failure_is_reported_without_its_detail(
    backup_host: BackupHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failed(_arguments: list[str] | None) -> int:
        raise RuntimeError("private-failure-detail")

    monkeypatch.setattr(backup, "main", failed)
    assert backup.run([]) == 1
    assert backup_host.events == [("pyracast_backup_failed", "ERROR", {})]
