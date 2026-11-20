import json
import shutil
import signal
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import FrameType, SimpleNamespace
from typing import IO, Any, NamedTuple

import pytest

from deploy import backup, host, rehearsal, reset_database

OBJECT = "postgres/2026/11/20/20261120T030000Z-hourly-0123456789abcdef.dump"
COUNTS = {"django_migrations": 40, "plants_plant": 3}
SECRETS = (
    "connections_provideraccount: 1 primary, 0 fallback, 0 unreadable\n"
    "mfa_authenticator: 0 primary, 1 fallback, 0 unreadable\n"
)
BOOTSTRAP = "-- bootstrap fixture\nSELECT 1;\n"
FLOOR = int(10**12 * backup.FREE_FLOOR_FRACTION)


class Usage(NamedTuple):
    total: int
    used: int
    free: int


class Docker:
    """A Docker host modelled at the command boundary the rehearsal uses."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.commands: list[list[str]] = []
        self.scripts: dict[int, str] = {}
        self.timeouts: list[float] = []
        self.overrides: list[str] = []
        self.restored: list[tuple[list[str], bytes, Path]] = []
        self.downloads: list[tuple[str, Path]] = []
        self.counts = "django_migrations 40\nplants_plant 3\n"
        self.secrets = SECRETS
        self.leftovers = {"ps": "", "volume": ""}
        self.failing: str | None = None
        self.on_restore: Callable[[], None] = lambda: None

    def fail(self, step: str) -> None:
        self.failing = step

    def capture(self, command: list[str], *, script: str = "", timeout: float = 1200) -> str:
        self.commands.append(command)
        self.timeouts.append(timeout)
        if script:
            self.scripts[len(self.commands) - 1] = script
        if command[:2] == ["docker", "compose"] and command[6:7] == ["-f"]:
            self.overrides.append(Path(command[7]).read_text(encoding="utf-8"))
        if self.failing == "roles" and script == reset_database.ROLE_CHECK_SQL:
            raise subprocess.CalledProcessError(3, command)
        if self.failing == "migrations" and "migrate" in command:
            raise subprocess.CalledProcessError(1, command)
        if self.failing == "secrets-error" and command[-1] == "check_secrets":
            raise subprocess.CalledProcessError(1, command)
        if command[:2] == ["docker", "info"]:
            return "/var/lib/docker\n"
        if command[-3:] == ["config", "--format", "json"]:
            return json.dumps({"services": {"postgres": {"image": "postgres@sha256:fixture"}}})
        if command[:2] == ["docker", "ps"]:
            return self.leftovers["ps"]
        if command[:3] == ["docker", "volume", "ls"]:
            return self.leftovers["volume"]
        if reset_database.COUNT_SQL in command:
            return self.counts
        if command[-1] == "check_secrets":
            return self.secrets
        return ""

    def bounded_run(
        self,
        command: list[str],
        *,
        stdin: IO[bytes],
        stdout: object,
        stderr: object,
        timeout: float,
        disk: Path,
    ) -> None:
        self.commands.append(command)
        self.timeouts.append(timeout)
        self.restored.append((command, stdin.read(), disk))
        self.on_restore()
        if self.failing == "restore":
            raise subprocess.CalledProcessError(1, command)

    def download(self, helper: reset_database.Host, name: str) -> Path:
        self.downloads.append((name, helper.state))
        if self.failing == "download":
            raise reset_database.RefusedError("checksum")
        archive = reset_database.archive_path(helper, name)
        archive.write_bytes(b"PGDMP-archive")
        return archive

    def started(self) -> list[str]:
        (start,) = [command for command in self.commands if command[:2] == ["docker", "run"]]
        return start

    def container(self) -> str:
        start = self.started()
        return start[start.index("--name") + 1]


class Rehearsed:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.release = root / "release"
        (self.release / "deploy").mkdir(parents=True)
        (self.release / "deploy/bootstrap.sql").write_text(BOOTSTRAP, encoding="utf-8")
        self.config = root / "host.json"
        self.pointer = root / "last-backup.json"
        self.status = root / "last-rehearsal.json"
        self.meminfo = root / "meminfo"
        self.meminfo.write_text("MemTotal: 4000000 kB\nMemAvailable: 2000000 kB\n")
        self.free = {"/var/lib/docker": 10**12, "directory": 10**12}
        self.docker = Docker(root)
        self.events: list[tuple[str, str, dict[str, object]]] = []
        self.handlers: dict[int, Callable[[int, FrameType | None], None]] = {}
        self.locked = True
        self.point()

    def point(self, **changes: object) -> None:
        pointer = {
            "object": OBJECT,
            "kind": "hourly",
            "size": 1000,
            "counts": COUNTS,
            "mark": {"boot_id": "boot-a", "uptime_seconds": 10.0},
        }
        self.pointer.write_text(json.dumps(pointer | changes))

    def helper(self, _config: Path) -> reset_database.Host:
        return reset_database.Host(
            config=self.config,
            release=self.release,
            bucket="private-backups",
            runtime_version="1",
            state=self.root / "reset",
        )

    def usage(self, path: Path | str) -> Usage:
        free = self.free.get(Path(path).as_posix(), self.free["directory"])
        return Usage(10**12, 10**12 - free, free)

    def report(self, event: str, severity: str, **details: object) -> None:
        self.events.append((event, severity, details))

    def run(self) -> int:
        return rehearsal.main([str(self.config)])

    def recorded(self) -> dict[str, Any]:
        status: dict[str, Any] = json.loads(self.status.read_text())
        return status


@pytest.fixture
def rehearsed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rehearsed:
    fake = Rehearsed(tmp_path)
    (tmp_path / "boot_id").write_text("boot-a\n")
    (tmp_path / "uptime").write_text("5000.00 0.00\n")
    monkeypatch.setattr(host, "BOOT_ID", tmp_path / "boot_id")
    monkeypatch.setattr(host, "UPTIME", tmp_path / "uptime")
    monkeypatch.setattr(host, "LAST_BACKUP", fake.pointer)
    monkeypatch.setattr(host, "LAST_REHEARSAL", fake.status)
    monkeypatch.setattr(host, "try_lock", lambda _stream: fake.locked)
    monkeypatch.setattr(rehearsal, "LOCK", tmp_path / "rehearsal.lock")
    monkeypatch.setattr(rehearsal, "MEMINFO", fake.meminfo)
    monkeypatch.setattr(rehearsal, "report", fake.report)
    monkeypatch.setattr(shutil, "disk_usage", fake.usage)
    monkeypatch.setattr(reset_database, "load_host", fake.helper)
    monkeypatch.setattr(reset_database, "capture", fake.docker.capture)
    monkeypatch.setattr(reset_database, "download_archive", fake.docker.download)
    monkeypatch.setattr(reset_database, "wait_until_ready", lambda _container: None)
    monkeypatch.setattr(backup, "bounded_run", fake.docker.bounded_run)
    monkeypatch.setattr(signal, "signal", fake.handlers.__setitem__)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    return fake


def forbidden(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("The rehearsal starts processes only through its own helpers.")


def index(commands: list[list[str]], predicate: Callable[[list[str]], bool]) -> int:
    return next(position for position, command in enumerate(commands) if predicate(command))


def test_the_rehearsal_restores_the_latest_backup_with_its_roles_and_records_the_recovery_time(
    rehearsed: Rehearsed,
) -> None:
    assert rehearsed.run() == 0

    docker = rehearsed.docker
    status = rehearsed.recorded()
    assert status.pop("recovery_seconds") >= 0
    assert status == {
        "status": "passed",
        "object": OBJECT,
        "tables": 2,
        "rows": 43,
        "secrets": 2,
        "mark": {"boot_id": "boot-a", "uptime_seconds": 5000.0},
    }
    ((event, severity, details),) = rehearsed.events
    assert (event, severity) == ("pyracast_rehearsal_passed", "INFO")
    assert details.keys() == {"object", "recovery_seconds", "tables", "rows", "secrets"}
    ((name, state),) = docker.downloads
    assert name == OBJECT
    assert state != rehearsed.root / "reset"
    assert not state.exists()
    container = docker.container()
    bootstraps = [position for position, script in docker.scripts.items() if script == BOOTSTRAP]
    (restore,) = [command for command, _, _ in docker.restored]
    assert restore == [
        "docker",
        "exec",
        "-i",
        container,
        "pg_restore",
        "-h",
        "127.0.0.1",
        "-U",
        "postgres",
        "-d",
        "pyracast",
        "--exit-on-error",
        "--single-transaction",
    ]
    restored_at = docker.commands.index(restore)
    assert len(bootstraps) == 2
    assert bootstraps[0] < restored_at < bootstraps[1]
    assert all("--single-transaction" in docker.commands[position] for position in bootstraps)
    (roles,) = [
        position
        for position, script in docker.scripts.items()
        if script == reset_database.ROLE_CHECK_SQL
    ]
    counted = index(docker.commands, lambda c: reset_database.COUNT_SQL in c)
    migrated = index(docker.commands, lambda c: c[-2:] == ["migrate", "--check"])
    decrypted = index(docker.commands, lambda c: c[-1] == "check_secrets")
    assert bootstraps[1] < roles < counted < migrated < decrypted
    assert docker.commands[-2:] == [
        ["docker", "rm", "--force", container],
        ["docker", "volume", "rm", f"{container}-data"],
    ]


def test_the_throwaway_database_has_no_network_bounded_resources_and_its_own_volume(
    rehearsed: Rehearsed,
) -> None:
    assert rehearsed.run() == 0

    start = rehearsed.docker.started()
    container = rehearsed.docker.container()
    option = {
        start[position]: start[position + 1] for position in range(2, start.index("--volume") + 2)
    }
    assert container.startswith("pyracast-rehearsal-")
    assert option["--network"] == "none"
    assert option["--memory"] == option["--memory-swap"] == f"{rehearsal.DATABASE_MEMORY_MB}m"
    assert option["--cpus"] == "0.5"
    assert option["--label"] == rehearsal.LABEL
    assert option["--volume"] == f"{container}-data:/var/lib/postgresql"
    assert start[start.index("--volume") + 2] == "postgres@sha256:fixture"
    created = ["docker", "volume", "create", "--label", rehearsal.LABEL, f"{container}-data"]
    assert created in rehearsed.docker.commands
    for command in rehearsed.docker.commands:
        joined = " ".join(command)
        assert "/var/lib/pyracast" not in joined
        if "compose" in command:
            action = command[command.index("compose") + 5 :]
            assert action[:1] == ["config"] or action[2:6] == ["run", "--rm", "--no-deps", "api"]


def test_the_probe_reaches_only_the_throwaway_and_takes_its_secrets_from_the_release(
    rehearsed: Rehearsed,
) -> None:
    assert rehearsed.run() == 0

    container = rehearsed.docker.container()
    probes = [
        command for command in rehearsed.docker.commands if "run" in command and "api" in command
    ]
    assert [command[command.index("api") + 1 :] for command in probes] == [
        ["pyracast", "migrate", "--check"],
        ["pyracast", "check_secrets"],
    ]
    for command in probes:
        assert not {"-e", "--env", "--env-file"} & set(command[command.index("run") :])
    assert (
        rehearsed.docker.overrides
        == [
            "services:\n"
            "  api:\n"
            f"    network_mode: container:{container}\n"
            "    networks: !reset []\n"
            "    environment:\n"
            "      PGHOST: 127.0.0.1\n"
            '      PGPORT: "5432"\n'
            f"    mem_limit: {rehearsal.PROBE_MEMORY_MB}m\n"
            "    cpus: 0.5\n"
        ]
        * 2
    )


def test_a_pre_release_backup_is_checked_for_a_migration_plan_the_release_can_apply(
    rehearsed: Rehearsed,
) -> None:
    rehearsed.point(kind="prerelease")

    assert rehearsed.run() == 0
    assert any(command[-2:] == ["migrate", "--plan"] for command in rehearsed.docker.commands)
    assert not any("--check" in command for command in rehearsed.docker.commands)


@pytest.mark.parametrize(
    ("failure", "step", "error"),
    [
        ("download", "download", "RefusedError"),
        ("restore", "restore", "CalledProcessError"),
        ("roles", "roles", "CalledProcessError"),
        ("counts", "counts", "RuntimeError"),
        ("migrations", "migrations", "CalledProcessError"),
        ("secrets-error", "secrets", "CalledProcessError"),
        ("secrets-none", "secrets", "RuntimeError"),
    ],
)
def test_a_failed_step_is_recorded_and_reported_and_the_throwaway_is_removed(
    rehearsed: Rehearsed, failure: str, step: str, error: str
) -> None:
    docker = rehearsed.docker
    docker.fail(failure)
    if failure == "counts":
        docker.counts = "django_migrations 40\nplants_plant 2\n"
    if failure == "secrets-none":
        docker.secrets = "connections_provideraccount: 0 primary, 0 fallback, 0 unreadable\n"

    assert rehearsed.run() == 1

    assert rehearsed.recorded() == {
        "status": "failed",
        "step": step,
        "mark": {"boot_id": "boot-a", "uptime_seconds": 5000.0},
    }
    assert rehearsed.events == [
        ("pyracast_rehearsal_failed", "WARNING", {"step": step, "error": error})
    ]
    if failure != "download":
        container = docker.container()
        assert docker.commands[-2:] == [
            ["docker", "rm", "--force", container],
            ["docker", "volume", "rm", f"{container}-data"],
        ]


@pytest.mark.parametrize(
    "shortage",
    ["memory", "docker", "download"],
)
def test_the_rehearsal_starts_nothing_without_room_beside_the_running_site(
    rehearsed: Rehearsed, shortage: str
) -> None:
    if shortage == "memory":
        rehearsed.meminfo.write_text("MemTotal: 4000000 kB\nMemAvailable: 600000 kB\n")
    elif shortage == "docker":
        rehearsed.free["/var/lib/docker"] = FLOOR + 3999
    else:
        rehearsed.free["directory"] = FLOOR + 999
        rehearsed.free["/var/lib/docker"] = 10**12

    assert rehearsed.run() == 1

    assert rehearsed.recorded()["step"] == "capacity"
    assert rehearsed.docker.downloads == []
    assert not any(command[:2] == ["docker", "run"] for command in rehearsed.docker.commands)
    assert not any(
        command[:3] == ["docker", "volume", "create"] for command in rehearsed.docker.commands
    )


def test_room_exactly_above_the_floor_is_enough(rehearsed: Rehearsed) -> None:
    rehearsed.free["/var/lib/docker"] = FLOOR + 4000
    rehearsed.free["directory"] = FLOOR + 1000

    assert rehearsed.run() == 0


def test_leftovers_of_a_killed_rehearsal_are_removed_before_a_new_throwaway_starts(
    rehearsed: Rehearsed,
) -> None:
    rehearsed.docker.leftovers = {"ps": "old-container\nother\n", "volume": "old-volume\n"}

    assert rehearsed.run() == 0

    commands = rehearsed.docker.commands
    label = f"label={rehearsal.LABEL}"
    assert commands[:4] == [
        ["docker", "ps", "--all", "--quiet", "--filter", label],
        ["docker", "rm", "--force", "old-container", "other"],
        ["docker", "volume", "ls", "--quiet", "--filter", label],
        ["docker", "volume", "rm", "old-volume"],
    ]


def test_nothing_is_removed_when_no_rehearsal_left_anything(rehearsed: Rehearsed) -> None:
    assert rehearsed.run() == 0

    assert [command[:3] for command in rehearsed.docker.commands[:2]] == [
        ["docker", "ps", "--all"],
        ["docker", "volume", "ls"],
    ]


@pytest.mark.parametrize(
    "pointer",
    [
        None,
        "not json",
        json.dumps({"object": "other/x.dump", "kind": "hourly", "size": 1, "counts": {}}),
        json.dumps({"object": OBJECT, "kind": "weekly", "size": 1, "counts": {}}),
        json.dumps({"object": OBJECT, "kind": "hourly", "size": 1}),
        json.dumps({"object": OBJECT, "completed_at": 0}),
    ],
)
def test_a_pointer_that_names_no_rehearsable_backup_fails_before_any_container_starts(
    rehearsed: Rehearsed, pointer: str | None
) -> None:
    if pointer is None:
        rehearsed.pointer.unlink()
    else:
        rehearsed.pointer.write_text(pointer)

    assert rehearsed.run() == 1

    assert rehearsed.recorded()["step"] == "pointer"
    assert rehearsed.docker.commands == []


def test_a_busy_rehearsal_lock_leaves_the_status_and_the_host_alone(
    rehearsed: Rehearsed, capsys: pytest.CaptureFixture[str]
) -> None:
    rehearsed.locked = False

    assert rehearsed.run() == 2

    assert not rehearsed.status.exists()
    assert rehearsed.docker.commands == []
    assert rehearsed.events == []
    assert "Another restore rehearsal" in capsys.readouterr().err


def test_a_rehearsal_stopped_by_systemd_removes_its_throwaway_and_records_the_stop(
    rehearsed: Rehearsed,
) -> None:
    def stopped() -> None:
        rehearsed.handlers[signal.SIGTERM](signal.SIGTERM, None)

    rehearsed.docker.on_restore = stopped

    with pytest.raises(SystemExit) as stop:
        rehearsed.run()

    assert stop.value.code == 143
    container = rehearsed.docker.container()
    assert rehearsed.docker.commands[-2:] == [
        ["docker", "rm", "--force", container],
        ["docker", "volume", "rm", f"{container}-data"],
    ]
    assert rehearsed.recorded()["step"] == "terminated"
    assert rehearsed.events == [
        ("pyracast_rehearsal_failed", "WARNING", {"step": "terminated", "error": "SystemExit"})
    ]


def test_every_command_gets_only_the_time_left_and_none_starts_after_the_deadline(
    rehearsed: Rehearsed, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [0.0]
    monkeypatch.setattr(rehearsal, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def late() -> None:
        now[0] = rehearsal.DEADLINE_SECONDS + 1

    rehearsed.docker.on_restore = late

    assert rehearsed.run() == 1

    assert rehearsed.recorded()["step"] == "restore"
    assert rehearsed.events[0][2] == {"step": "restore", "error": "TimeoutExpired"}
    assert all(0 < timeout <= rehearsal.DEADLINE_SECONDS for timeout in rehearsed.docker.timeouts)
    container = rehearsed.docker.container()
    assert rehearsed.docker.commands[-2:] == [
        ["docker", "rm", "--force", container],
        ["docker", "volume", "rm", f"{container}-data"],
    ]


def test_a_cleanup_that_fails_does_not_change_the_outcome(
    rehearsed: Rehearsed, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = rehearsed.docker.capture

    def failing_cleanup(command: list[str], *, script: str = "", timeout: float = 1200) -> str:
        if command[:3] in (["docker", "rm", "--force"], ["docker", "volume", "rm"]):
            raise subprocess.CalledProcessError(1, command)
        return capture(command, script=script, timeout=timeout)

    monkeypatch.setattr(reset_database, "capture", failing_cleanup)

    assert rehearsed.run() == 0
    assert rehearsed.recorded()["status"] == "passed"
