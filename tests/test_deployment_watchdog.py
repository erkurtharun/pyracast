import shutil
import subprocess
from pathlib import Path
from typing import NamedTuple

import pytest

from deploy import watchdog


@pytest.mark.parametrize(
    ("boot_free", "data_free", "memory_free", "warns"),
    [(10, 50, 50, True), (50, 10, 50, True), (50, 50, 5, True), (50, 50, 50, False)],
)
def test_capacity_warning_covers_boot_disk_data_disk_and_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boot_free: int,
    data_free: int,
    memory_free: int,
    warns: bool,
) -> None:
    class Usage(NamedTuple):
        total: int
        used: int
        free: int

    memory = tmp_path / "meminfo"
    memory.write_text(f"MemTotal: 100 kB\nMemAvailable: {memory_free} kB\n")
    events: list[dict[str, object]] = []

    def disk_usage(path: str) -> Usage:
        free = boot_free if path == "/" else data_free
        return Usage(100, 100 - free, free)

    def report(event: str, severity: str, **details: object) -> None:
        events.append({"event": event, "severity": severity, **details})

    monkeypatch.setattr(shutil, "disk_usage", disk_usage)
    monkeypatch.setattr(watchdog, "Path", lambda _value: memory)
    monkeypatch.setattr(watchdog, "report", report)
    watchdog.check_capacity()
    if warns:
        assert events == [
            {
                "event": "pyracast_host_capacity",
                "severity": "WARNING",
                "boot_disk_free_percent": float(boot_free),
                "data_disk_free_percent": float(data_free),
                "memory_available_percent": float(memory_free),
            }
        ]
    else:
        assert events == []


@pytest.mark.parametrize("active", ["active", "inactive", "activating", "deactivating"])
def test_watchdog_recovers_exhausted_worker_only_while_service_is_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    active: str,
) -> None:
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy/release.env").touch()
    commands: list[list[str]] = []
    events: list[str] = []
    snapshots = [
        {"worker": "exited", "dispatcher": "running"},
        {"worker": "running", "dispatcher": "running"},
    ]

    def path(value: str) -> Path:
        if value.startswith("/var/lib/pyracast/"):
            return tmp_path / value.rsplit("/", 1)[-1]
        return Path(value)

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, active if command[0] == "systemctl" else "")

    def inspect(_compose: list[str]) -> dict[str, str]:
        return snapshots.pop(0)

    def report(event: str, _severity: str, **_details: object) -> None:
        events.append(event)

    monkeypatch.setattr(watchdog, "Path", path)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(watchdog, "inspect_services", inspect)
    monkeypatch.setattr(watchdog, "report", report)
    monkeypatch.setattr(watchdog, "check_capacity", lambda: None)
    watchdog.supervise(tmp_path)
    if active == "active":
        assert any(command[-2:] == ["restart", "worker"] for command in commands)
        assert commands[-1][-4:] == ["-T", "api", "pyracast", "check_delivery_health"]
        assert events == ["pyracast_runtime_restarted"]
    else:
        assert len(commands) == 1
        assert events == []


def test_watchdog_never_recovers_a_migration_blocked_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked = tmp_path / "release-blocked"
    blocked.touch()
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "active")

    monkeypatch.setattr(watchdog, "Path", lambda _value: blocked)
    monkeypatch.setattr(subprocess, "run", run)
    watchdog.supervise(tmp_path)
    assert commands == []
