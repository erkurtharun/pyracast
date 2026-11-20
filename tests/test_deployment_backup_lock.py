import json
import re
import signal
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any, BinaryIO

import pytest

from deploy import backup, host, release_record

COUNTS = {"django_migrations": 40, "plants_plant": 3}


class BackupHost:
    def __init__(self, root: Path) -> None:
        self.release = root / "release"
        self.release.mkdir()
        self.directory = root / "work"
        self.directory.mkdir()
        self.kept = root / "reset"
        self.blocked = root / "release-blocked"
        self.last_backup = root / "last-backup.json"
        self.boot_id = root / "boot_id"
        self.boot_id.write_text("boot-a\n")
        self.uptime = root / "uptime"
        self.uptime.write_text("5000.00 9000.00\n")
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
        self.replaced: list[tuple[str, bool]] = []
        self.lock_holder: int | None = 9
        self.checked: list[int] = []

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

    def holds_release_lock(self, descriptor: int) -> bool:
        self.checked.append(descriptor)
        return descriptor == self.lock_holder

    def run(self, command: list[str], **options: Any) -> subprocess.CompletedProcess[bytes]:
        self.commands.append(command)
        if "pg_dump" in command:
            self.dumps.append((command, self.held))
            if self.dump_error is not None:
                raise self.dump_error
            stream: BinaryIO = options["stdout"]
            stream.write(b"PGDMP")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    def bounded_run(
        self, command: list[str], *, stdout: IO[bytes], stderr: IO[bytes], **_options: Any
    ) -> None:
        self.commands.append(command)
        self.dumps.append((command, self.held))
        if self.dump_error is not None:
            raise self.dump_error
        stdout.write(b"PGDMP")
        stderr.write(f"pyracast-counts {json.dumps(COUNTS)}\n".encode())

    def upload(self, _bucket: str, name: str, archive: Path) -> None:
        assert archive.read_bytes() == b"PGDMP"
        self.uploads.append((name, self.held))

    def report(self, event: str, severity: str, **details: object) -> None:
        self.events.append((event, severity, details))


@pytest.fixture
def backup_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BackupHost:
    fake = BackupHost(tmp_path)
    monkeypatch.setattr(host, "RELEASE_BLOCKED", fake.blocked)
    monkeypatch.setattr(host, "LAST_BACKUP", fake.last_backup)
    monkeypatch.setattr(host, "RECOVERY_STATE", fake.kept, raising=False)
    monkeypatch.setattr(host, "BOOT_ID", fake.boot_id, raising=False)
    monkeypatch.setattr(host, "UPTIME", fake.uptime, raising=False)
    monkeypatch.setattr(host, "release_lock", fake.release_lock)
    monkeypatch.setattr(host, "holds_release_lock", fake.holds_release_lock)
    monkeypatch.setattr(backup, "upload_backup", fake.upload)
    monkeypatch.setattr(backup, "report", fake.report)
    monkeypatch.setattr(backup, "bounded_run", fake.bounded_run, raising=False)
    monkeypatch.setattr(subprocess, "run", fake.run)
    monkeypatch.setattr(signal, "signal", fake.handlers.__setitem__)
    replace = Path.replace

    def replace_watched(source: Path, target: Path) -> Path:
        fake.replaced.append((target.name, fake.held))
        return replace(source, target)

    monkeypatch.setattr(Path, "replace", replace_watched)
    return fake


def test_hourly_backup_holds_the_release_lock_until_the_pointer_names_its_upload(
    backup_host: BackupHost,
) -> None:
    name = backup.create_backup(
        backup_host.release, "private-backups", "hourly", backup_host.directory
    )

    assert backup_host.waits == [backup.HOURLY_LOCK_WAIT_SECONDS]
    assert [held for _, held in backup_host.dumps] == [True]
    assert backup_host.uploads == [(name, True)]
    assert backup_host.replaced[-1] == ("last-backup.json", True)
    assert all(held for _, held in backup_host.replaced)
    assert name is not None
    assert re.fullmatch(r"postgres/\d{4}/\d{2}/\d{2}/\d{8}T\d{6}Z-hourly-[0-9a-f]{32}\.dump", name)
    marker = json.loads(backup_host.last_backup.read_text())
    assert marker["object"] == name
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


def test_prerelease_backup_after_an_hourly_one_leaves_the_pointer_on_the_prerelease_archive(
    backup_host: BackupHost,
) -> None:
    hourly = backup.create_backup(
        backup_host.release, "private-backups", "hourly", backup_host.directory
    )
    prerelease = backup.create_backup(
        backup_host.release, "private-backups", "prerelease", backup_host.directory, 9
    )

    assert hourly is not None
    assert prerelease is not None
    pointer = json.loads(backup_host.last_backup.read_text())
    assert pointer.keys() == {"object", "kind", "size", "counts", "mark"}
    assert pointer["object"] == prerelease
    assert pointer["kind"] == "prerelease"


@pytest.mark.parametrize("kind", ["hourly", "prerelease"])
def test_the_pointer_names_the_dump_its_counts_and_an_uptime_mark_but_no_wall_clock_time(
    backup_host: BackupHost, kind: str
) -> None:
    name = backup.create_backup(
        backup_host.release, "private-backups", kind, backup_host.directory, 9
    )

    assert json.loads(backup_host.last_backup.read_text()) == {
        "object": name,
        "kind": kind,
        "size": 5,
        "counts": COUNTS,
        "mark": {"boot_id": "boot-a", "uptime_seconds": 5000.0},
    }


def test_prerelease_backup_records_its_object_in_the_release_directory(
    backup_host: BackupHost,
) -> None:
    name = backup.create_backup(
        backup_host.release, "private-backups", "prerelease", backup_host.directory, 9
    )

    record = backup_host.release / host.PRERELEASE_RECORD
    assert json.loads(record.read_text()) == {"object": name, "counts": COUNTS}
    assert [path.name for path in backup_host.release.iterdir()] == [record.name]


def test_prerelease_backup_keeps_its_dump_where_the_recovery_helper_looks(
    backup_host: BackupHost,
) -> None:
    name = backup.create_backup(
        backup_host.release, "private-backups", "prerelease", backup_host.directory, 9
    )

    assert name is not None
    kept = backup_host.kept / name.rsplit("/", 1)[-1]
    assert kept.read_bytes() == b"PGDMP"
    assert sorted(path.name for path in backup_host.kept.iterdir()) == [kept.name]
    assert list(backup_host.directory.iterdir()) == []


def test_an_hourly_dump_stays_in_its_temporary_directory(backup_host: BackupHost) -> None:
    backup.create_backup(backup_host.release, "private-backups", "hourly", backup_host.directory)

    assert not backup_host.kept.exists()


def test_a_prerelease_dump_that_cannot_be_kept_fails_before_anything_is_uploaded(
    backup_host: BackupHost,
) -> None:
    backup_host.kept.write_text("not a directory")

    with pytest.raises((FileExistsError, NotADirectoryError)):
        backup.create_backup(
            backup_host.release, "private-backups", "prerelease", backup_host.directory, 9
        )

    assert backup_host.uploads == []
    assert not backup_host.last_backup.exists()
    assert list(backup_host.release.iterdir()) == []


@pytest.mark.parametrize("failure", ["dump", "upload"])
def test_a_failed_prerelease_backup_keeps_no_archive(
    backup_host: BackupHost, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    def refused(_bucket: str, _name: str, _archive: Path) -> None:
        raise OSError("upload refused")

    if failure == "dump":
        backup_host.dump_error = subprocess.CalledProcessError(3, ["psql"])
    else:
        monkeypatch.setattr(backup, "upload_backup", refused)

    with pytest.raises((OSError, subprocess.CalledProcessError)):
        backup.create_backup(
            backup_host.release, "private-backups", "prerelease", backup_host.directory, 9
        )

    assert list(backup_host.kept.iterdir()) == []
    assert not backup_host.last_backup.exists()


def test_hourly_backup_writes_no_release_record(backup_host: BackupHost) -> None:
    backup.create_backup(backup_host.release, "private-backups", "hourly", backup_host.directory)

    assert list(backup_host.release.iterdir()) == []


def test_prerelease_backup_never_waits_for_the_lock_its_installer_holds(
    backup_host: BackupHost,
) -> None:
    name = backup.create_backup(
        backup_host.release, "private-backups", "prerelease", backup_host.directory, 9
    )

    assert backup_host.waits == []
    assert backup_host.checked == [9]
    assert len(backup_host.dumps) == 1
    assert name is not None
    assert "-prerelease-" in name


@pytest.mark.parametrize("descriptor", [None, 7])
def test_prerelease_backup_refuses_unless_its_caller_holds_the_release_lock(
    backup_host: BackupHost, descriptor: int | None
) -> None:
    with pytest.raises(backup.ReleaseLockNotHeldError):
        backup.create_backup(
            backup_host.release, "private-backups", "prerelease", backup_host.directory, descriptor
        )

    assert backup_host.dumps == []
    assert backup_host.uploads == []
    assert not backup_host.last_backup.exists()
    assert list(backup_host.release.iterdir()) == []
    assert backup_host.events == []


@pytest.mark.parametrize("descriptor", [[], ["--lock-fd", "7"]])
def test_a_prerelease_run_outside_a_release_is_refused_and_changes_nothing(
    backup_host: BackupHost,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    descriptor: list[str],
) -> None:
    configuration = tmp_path / "host.json"
    configuration.write_text(json.dumps({"backup_bucket": "private-backups"}))

    status = backup.run(
        [
            str(configuration),
            "--release-dir",
            str(backup_host.release),
            *descriptor,
            "--kind",
            "prerelease",
        ]
    )

    assert status == 2
    assert capsys.readouterr().err == (
        "Refused: a prerelease backup runs only inside a release, which passes the release "
        "lock it holds with --lock-fd. Nothing was dumped.\n"
    )
    assert backup_host.dumps == []
    assert not backup_host.last_backup.exists()
    assert backup_host.events == []


def test_a_prerelease_run_under_the_release_lock_succeeds(
    backup_host: BackupHost, tmp_path: Path
) -> None:
    configuration = tmp_path / "host.json"
    configuration.write_text(json.dumps({"backup_bucket": "private-backups"}))
    arguments = [
        "--release-dir",
        str(backup_host.release),
        "--lock-fd",
        "9",
        "--kind",
        "prerelease",
    ]

    assert backup.run([str(configuration), *arguments]) == 0
    assert backup_host.checked == [9]
    assert (backup_host.release / host.PRERELEASE_RECORD).exists()


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


VALID = "postgres/2026/11/18/20261118T010203Z-prerelease-0123456789abcdef.dump"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (json.dumps({"object": VALID}), VALID),
        (json.dumps({"object": VALID + "\n"}), None),
        (json.dumps({"object": "postgres/x\u0000.dump"}), None),
        (json.dumps({"object": "postgres/../x.dump"}), None),
        (json.dumps({"object": "postgres/./x.dump"}), None),
        (json.dumps({"object": "postgres/x.dump; touch /tmp/x"}), None),
        (json.dumps({"object": 123}), None),
        (json.dumps({"object": ["postgres/x.dump"]}), None),
        (json.dumps({}), None),
        (json.dumps(["postgres/x.dump"]), None),
        ("not json", None),
        (None, None),
    ],
)
def test_release_record_yields_only_a_valid_object_name(
    tmp_path: Path, content: str | None, expected: str | None
) -> None:
    record = tmp_path / host.PRERELEASE_RECORD
    if content is not None:
        record.write_text(content, encoding="utf-8")

    assert release_record.object_name(record) == expected
