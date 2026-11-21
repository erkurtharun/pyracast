import datetime
import json
import os
import shutil
import socket
import ssl
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple, cast

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from deploy import host, probe_origin_pull, watchdog

HEALTHY = dict.fromkeys(watchdog.SERVICES, "running")
DAY = 86400
UPTIME = 30 * DAY


def states(**changes: str) -> dict[str, str]:
    return {**HEALTHY, **changes}


def container(service: str, state: str) -> dict[str, object]:
    status: dict[str, object] = {"Status": state}
    if state in ("healthy", "unhealthy", "starting"):
        status = {"Status": "running", "Health": {"Status": state}}
    return {"Config": {"Labels": {"com.docker.compose.service": service}}, "State": status}


class Usage(NamedTuple):
    total: int
    used: int
    free: int


class Host:
    def __init__(self, root: Path) -> None:
        self.release = root / "release"
        (self.release / "deploy").mkdir(parents=True)
        (self.release / "deploy/release.env").write_text("SITE_ADDRESS=example.com\n")
        self.blocked = root / "release-blocked"
        self.boot_id = root / "boot_id"
        self.boot_id.write_text("boot-a\n")
        self.uptime = root / "uptime"
        self.uptime.write_text(f"{UPTIME:.2f} 0.00\n")
        self.last_backup = root / "last-backup.json"
        self.backed_up(age=60)
        self.last_rehearsal = root / "last-rehearsal.json"
        self.rehearsed("passed", age=DAY)
        self.delivery_checked = root / "delivery-checked"
        self.meminfo = root / "meminfo"
        self.meminfo.write_text("MemTotal: 100 kB\nMemAvailable: 50 kB\n")
        self.free = {"/": 50, "/var/lib/pyracast": 50}
        self.unit = "active"
        self.snapshots = [HEALTHY]
        self.delivery: int | BaseException = 0
        self.delivery_output = "{}"
        self.lock_free = True
        self.held = False
        self.on_lock: Callable[[], None] = lambda: None
        self.commands: list[list[str]] = []
        self.locked: list[list[str]] = []
        self.timeouts: list[float] = []
        self.events: list[tuple[str, str, dict[str, object]]] = []

    def backed_up(self, *, age: float, boot: str = "boot-a") -> None:
        mark = {"boot_id": boot, "uptime_seconds": UPTIME - age}
        self.last_backup.write_text(json.dumps({"object": "postgres/x.dump", "mark": mark}))

    def rehearsed(self, status: str, *, age: float, boot: str = "boot-a") -> None:
        mark = {"boot_id": boot, "uptime_seconds": UPTIME - age}
        self.last_rehearsal.write_text(
            json.dumps({"status": status, "step": "counts", "mark": mark})
        )

    def run(self, command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if self.held:
            self.locked.append(command)
        self.timeouts.append(options["timeout"])
        if command[0] == "systemctl":
            code = 0 if self.unit == "active" else 3
            return subprocess.CompletedProcess(command, code, f"{self.unit}\n", "")
        if command[-3:] == ["ps", "--all", "--quiet"]:
            return subprocess.CompletedProcess(command, 0, "container\n", "")
        if command[:2] == ["docker", "inspect"]:
            current = self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]
            containers = [
                container(service, state)
                for service, state in current.items()
                if state != "missing"
            ]
            return subprocess.CompletedProcess(command, 0, json.dumps(containers), "")
        if "check_delivery_health" in command:
            if isinstance(self.delivery, BaseException):
                raise self.delivery
            return subprocess.CompletedProcess(command, self.delivery, self.delivery_output, "")
        return subprocess.CompletedProcess(command, 0, "", "")

    @contextmanager
    def release_lock(self, wait_seconds: float) -> Iterator[bool]:
        assert wait_seconds == 0
        if not self.lock_free:
            yield False
            return
        self.held = True
        self.on_lock()
        try:
            yield True
        finally:
            self.held = False

    def report(self, event: str, severity: str, **details: object) -> None:
        self.events.append((event, severity, details))

    def names(self) -> list[str]:
        return [event for event, _, _ in self.events]

    def details(self, name: str) -> list[dict[str, object]]:
        return [details for event, _, details in self.events if event == name]

    def heartbeat(self) -> dict[str, object]:
        assert self.events[-1][:2] == ("pyracast_watchdog_heartbeat", "INFO")
        return self.events[-1][2]

    def compose(self, *arguments: str) -> list[list[str]]:
        return [
            command for command in self.commands if command[-len(arguments) :] == list(arguments)
        ]

    def starts(self) -> list[list[str]]:
        return [
            command[index:]
            for command in self.commands
            for index, part in enumerate(command)
            if part in ("restart", "up")
        ]


@pytest.fixture
def watched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Host:
    fake = Host(tmp_path)
    monkeypatch.setattr(host, "CURRENT_RELEASE", fake.release)
    monkeypatch.setattr(host, "RELEASE_BLOCKED", fake.blocked)
    monkeypatch.setattr(host, "LAST_BACKUP", fake.last_backup)
    monkeypatch.setattr(host, "LAST_REHEARSAL", fake.last_rehearsal)
    monkeypatch.setattr(host, "BOOT_ID", fake.boot_id)
    monkeypatch.setattr(host, "UPTIME", fake.uptime)
    monkeypatch.setattr(host, "release_lock", fake.release_lock)
    monkeypatch.setattr(watchdog, "DELIVERY_CHECKED", fake.delivery_checked)
    monkeypatch.setattr(watchdog, "MEMINFO", fake.meminfo)
    monkeypatch.setattr(watchdog, "report", fake.report)
    monkeypatch.setattr(subprocess, "run", fake.run)
    monkeypatch.setattr(
        shutil, "disk_usage", lambda path: Usage(100, 100 - fake.free[path], fake.free[path])
    )
    monkeypatch.setattr(watchdog, "certificate_validity", lambda *_args: (60 * DAY, 90 * DAY))
    return fake


def test_timed_out_delivery_check_still_checks_backup_age_and_capacity(watched: Host) -> None:
    watched.delivery = subprocess.TimeoutExpired(["check_delivery_health"], 60)
    watched.free["/var/lib/pyracast"] = 5
    watched.backed_up(age=2 * 3600)

    assert watchdog.main() == 1
    assert watched.names() == [
        "pyracast_host_capacity",
        "pyracast_backup_failed",
        "pyracast_watchdog_step_failed",
        "pyracast_watchdog_heartbeat",
    ]
    assert watched.details("pyracast_watchdog_step_failed") == [
        {"step": "delivery", "outcome": "timeout"}
    ]
    assert watched.heartbeat() == {"state": "ok"}


def test_unrecovered_runtime_is_reported_and_every_other_check_still_runs(
    watched: Host,
) -> None:
    watched.snapshots = [states(worker="exited")]
    watched.free["/"] = 5

    assert watchdog.main() == 0
    assert watched.compose("restart", "--timeout", "60", "worker")
    assert watched.details("pyracast_runtime_unhealthy") == [{"services": {"worker": "exited"}}]
    assert "pyracast_host_capacity" in watched.names()
    assert watched.compose("pyracast", "check_delivery_health")
    assert watched.heartbeat() == {"state": "ok"}


def test_a_failing_compose_call_is_reported_without_skipping_other_checks(
    watched: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        if command[-3:] == ["ps", "--all", "--quiet"]:
            raise subprocess.CalledProcessError(1, command, "private-output")
        return watched.run(command, **options)

    monkeypatch.setattr(subprocess, "run", broken)
    watched.free["/"] = 5

    assert watchdog.main() == 1
    assert watched.details("pyracast_watchdog_step_failed") == [
        {"step": "runtime", "outcome": "error", "error_type": "CalledProcessError"}
    ]
    assert "pyracast_host_capacity" in watched.names()
    assert watched.compose("pyracast", "check_delivery_health")


def test_application_services_restart_under_the_release_lock_only(watched: Host) -> None:
    watched.snapshots = [states(worker="exited"), states(worker="exited"), HEALTHY]

    assert watchdog.main() == 0
    assert watched.details("pyracast_runtime_restarted") == [{"services": ["worker"]}]
    locked = [command[-1] for command in watched.locked]
    assert locked[:3] == ["pyracast.service", "--quiet", "container"]
    assert watched.compose("restart", "--timeout", "60", "worker")[0] in watched.locked
    assert any(
        command[-7:-2] == ["up", "-d", "--no-deps", "--wait", "--wait-timeout"]
        for command in watched.locked
    )
    unlocked = [command for command in watched.commands if command not in watched.locked]
    assert watched.compose("pyracast", "check_delivery_health")[0] in unlocked
    assert watched.heartbeat() == {"state": "ok"}


def test_busy_release_lock_skips_recovery_and_says_so(watched: Host) -> None:
    watched.snapshots = [states(dispatcher="unhealthy")]
    watched.lock_free = False

    assert watchdog.main() == 0
    assert not watched.compose("restart", "--timeout", "60", "dispatcher")
    assert watched.heartbeat() == {"state": "lock_busy"}


@pytest.mark.parametrize(
    ("change", "heartbeat"),
    [
        ("blocked", {"state": "blocked"}),
        ("unit", {"state": "unit_failed", "unit_state": "deactivating"}),
        ("recovered", {"state": "ok"}),
    ],
)
def test_recovery_checks_the_marker_unit_and_containers_again_inside_the_lock(
    watched: Host, change: str, heartbeat: dict[str, object]
) -> None:
    watched.snapshots = [states(api="unhealthy"), states(api="unhealthy")]

    def changed() -> None:
        if change == "blocked":
            watched.blocked.touch()
        elif change == "unit":
            watched.unit = "deactivating"
        else:
            watched.snapshots = [HEALTHY]

    watched.on_lock = changed
    watchdog.main()
    assert not any("restart" in command for command in watched.commands)
    assert "pyracast_runtime_restarted" not in watched.names()
    assert watched.heartbeat() == heartbeat


def test_unhealthy_support_service_is_reported_and_left_to_docker(watched: Host) -> None:
    watched.snapshots = [states(postgres="unhealthy")]

    assert watchdog.main() == 0
    assert not any("restart" in command or "up" in command for command in watched.commands)
    assert watched.details("pyracast_runtime_unhealthy") == [
        {"services": {"postgres": "unhealthy"}}
    ]


@pytest.mark.parametrize(
    ("failure", "restarted", "started"),
    [
        (states(postgres="exited", worker="exited"), ["worker"], ["worker"]),
        (states(rabbitmq="exited", api="unhealthy"), ["api"], ["api"]),
        (states(rabbitmq="missing", worker="exited"), ["worker"], ["rabbitmq", "worker"]),
    ],
)
def test_recovery_starts_only_the_failed_services_and_never_a_stopped_support_service(
    watched: Host, failure: dict[str, str], restarted: list[str], started: list[str]
) -> None:
    watched.snapshots = [failure, failure, HEALTHY]

    assert watchdog.main() == 0
    restart, start = watched.starts()
    assert restart == ["restart", "--timeout", "60", *restarted]
    assert start[:5] == ["up", "-d", "--no-deps", "--wait", "--wait-timeout"]
    assert start[6:] == started


def test_missing_containers_are_created_again(watched: Host) -> None:
    watched.snapshots = [states(rabbitmq="missing"), states(rabbitmq="missing"), HEALTHY]

    assert watchdog.main() == 0
    assert not any("restart" in command for command in watched.commands)
    assert watched.details("pyracast_runtime_restarted") == [{"services": ["rabbitmq"]}]


def test_blocked_release_reports_heartbeat_and_capacity_without_touching_containers(
    watched: Host,
) -> None:
    watched.blocked.touch()
    watched.free["/"] = 5
    watched.backed_up(age=3 * 3600)
    watched.rehearsed("failed", age=DAY)

    assert watchdog.main() == 0
    assert watched.commands == []
    assert watched.names() == ["pyracast_host_capacity", "pyracast_watchdog_heartbeat"]
    assert watched.heartbeat() == {"state": "blocked"}


@pytest.mark.parametrize(
    ("age", "events"),
    [
        (3600 - 60, []),
        (3600 + 180, [{"hours": 1.1}]),
        (5 * DAY, [{"hours": 120.0}]),
    ],
)
def test_a_release_blocked_for_longer_than_an_hour_is_reported_with_its_age(
    watched: Host, age: float, events: list[dict[str, object]]
) -> None:
    watched.blocked.touch()
    stamp = time.time() - age
    os.utime(watched.blocked, (stamp, stamp))

    assert watchdog.main() == 0
    assert watched.details("pyracast_release_blocked") == events
    assert watched.heartbeat() == {"state": "blocked"}


def test_a_block_that_ends_while_it_is_checked_is_not_reported(
    watched: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    def ended() -> os.stat_result:
        raise FileNotFoundError(watched.blocked)

    monkeypatch.setattr(host, "RELEASE_BLOCKED", SimpleNamespace(exists=lambda: True, stat=ended))

    assert watchdog.main() == 0
    assert watched.names() == ["pyracast_watchdog_heartbeat"]
    assert watched.heartbeat() == {"state": "blocked"}


@pytest.mark.parametrize("unit", ["inactive", "failed", "activating"])
def test_inactive_application_unit_is_reported_without_recovery(watched: Host, unit: str) -> None:
    watched.unit = unit
    watched.snapshots = [states(worker="exited")]
    watched.backed_up(age=3 * 3600)

    assert watchdog.main() == 0
    assert [command[0] for command in watched.commands] == ["systemctl"]
    assert watched.names() == ["pyracast_backup_failed", "pyracast_watchdog_heartbeat"]
    assert watched.heartbeat() == {"state": "unit_failed", "unit_state": unit}


@pytest.mark.parametrize(("age", "runs"), [(60, False), (watchdog.DELIVERY_INTERVAL_SECONDS, True)])
def test_delivery_check_runs_at_most_every_five_minutes(
    watched: Host, age: float, runs: bool
) -> None:
    watched.delivery_checked.touch()
    stamp = time.time() - age
    os.utime(watched.delivery_checked, (stamp, stamp))
    watched.delivery = 1

    watchdog.main()
    assert bool(watched.compose("pyracast", "check_delivery_health")) is runs
    assert bool(watched.details("pyracast_delivery_unhealthy")) is runs
    assert (watched.delivery_checked.stat().st_mtime > stamp + 1) is runs


def test_first_delivery_check_runs_and_records_its_time(watched: Host) -> None:
    watchdog.main()
    assert watched.compose("pyracast", "check_delivery_health")
    assert watched.delivery_checked.exists()
    assert "pyracast_delivery_unhealthy" not in watched.names()


def test_an_unhealthy_delivery_report_names_its_failing_checks_and_counts(watched: Host) -> None:
    watched.delivery = 1
    watched.delivery_output = json.dumps(
        {
            "healthy": False,
            "alarms": {"overdue_collections": 2, "failed_calibrations": 1},
            "overdue_collections": 2,
            "paused_notifications": 4,
            "unhealthy_dispatch_phases": ["collection"],
            "broker": {
                "reachable": True,
                "queues": {
                    "collections": {"missing": True, "messages": 0},
                    "notifications": {"missing": False, "messages": 3},
                },
            },
        }
    )

    assert watchdog.main() == 0
    assert watched.details("pyracast_delivery_unhealthy") == [
        {
            "alarms": {"overdue_collections": 2, "failed_calibrations": 1},
            "dispatch_phases": ["collection"],
            "broker_unreachable": False,
            "missing_queues": ["collections"],
        }
    ]
    assert "pyracast_runtime_unhealthy" not in watched.names()


def test_an_unreachable_broker_is_named_by_the_delivery_alert(watched: Host) -> None:
    watched.delivery = 1
    watched.delivery_output = json.dumps(
        {"alarms": {}, "unhealthy_dispatch_phases": [], "broker": {"reachable": False}}
    )

    watchdog.main()
    assert watched.details("pyracast_delivery_unhealthy") == [
        {"alarms": {}, "dispatch_phases": [], "broker_unreachable": True, "missing_queues": []}
    ]


@pytest.mark.parametrize(
    "output",
    [
        "",
        "not json",
        "[]",
        '{"unhealthy_dispatch_phases": [], "broker": {"reachable": true}}',
        '{"alarms": [], "unhealthy_dispatch_phases": [], "broker": {"reachable": true}}',
        '{"alarms": {}, "unhealthy_dispatch_phases": "collection", "broker": {}}',
        '{"alarms": {}, "unhealthy_dispatch_phases": [], "broker": "down"}',
    ],
)
def test_a_delivery_check_without_a_readable_report_says_so(watched: Host, output: str) -> None:
    watched.delivery = 1
    watched.delivery_output = output

    assert watchdog.main() == 0
    assert watched.details("pyracast_delivery_unhealthy") == [
        {"outcome": "unreadable", "exit_status": 1}
    ]


def step_wall_clock(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    stepped = time.time() + seconds
    monkeypatch.setattr(time, "time", lambda: stepped)


@pytest.mark.parametrize("step", [-DAY, 0, DAY])
@pytest.mark.parametrize(("age", "stale"), [(60, False), (2 * 3600, True)])
def test_backup_age_is_measured_in_uptime_so_a_clock_step_neither_hides_nor_fakes_staleness(
    watched: Host, monkeypatch: pytest.MonkeyPatch, step: float, age: float, stale: bool
) -> None:
    mark = {"boot_id": "boot-a", "uptime_seconds": UPTIME - age}
    watched.last_backup.write_text(json.dumps({"object": "postgres/x.dump", "mark": mark}))
    step_wall_clock(monkeypatch, step)

    watchdog.main()
    assert ("pyracast_backup_failed" in watched.names()) is stale


@pytest.mark.parametrize(("uptime", "stale"), [(600, False), (3 * 3600, True)])
@pytest.mark.parametrize(
    "pointer",
    [
        {"object": "postgres/x.dump", "mark": {"boot_id": "boot-z", "uptime_seconds": 10}},
        {"object": "postgres/x.dump", "completed_at": 0},
        ["not", "a", "pointer"],
        "not json",
        None,
    ],
)
def test_a_backup_not_recorded_in_this_boot_is_stale_once_the_boot_outlasts_the_bound(
    watched: Host, pointer: object, uptime: float, stale: bool
) -> None:
    watched.uptime.write_text(f"{uptime:.2f} 0.00\n")
    if pointer is None:
        watched.last_backup.unlink()
    else:
        text = pointer if isinstance(pointer, str) else json.dumps(pointer)
        watched.last_backup.write_text(text)

    watchdog.main()
    assert ("pyracast_backup_failed" in watched.names()) is stale


def rehearsal_events(watched: Host) -> list[tuple[str, dict[str, object]]]:
    return [
        (severity, details)
        for event, severity, details in watched.events
        if event == "pyracast_rehearsal_unhealthy"
    ]


@pytest.mark.parametrize("step", [-30 * DAY, 0, 30 * DAY])
@pytest.mark.parametrize(
    ("status", "age", "expected"),
    [
        ("passed", DAY, []),
        ("passed", 9 * DAY, [("WARNING", {"state": "overdue"})]),
        ("failed", DAY, [("WARNING", {"state": "failed", "step": "counts"})]),
        ("failed", 9 * DAY, [("WARNING", {"state": "failed", "step": "counts"})]),
    ],
)
def test_the_rehearsal_status_is_reported_unless_a_recent_rehearsal_passed(
    watched: Host,
    monkeypatch: pytest.MonkeyPatch,
    step: float,
    status: str,
    age: float,
    expected: list[tuple[str, dict[str, object]]],
) -> None:
    watched.rehearsed(status, age=age)
    step_wall_clock(monkeypatch, step)

    watchdog.main()
    assert rehearsal_events(watched) == expected


@pytest.mark.parametrize("uptime", [DAY, 9 * DAY])
def test_a_host_without_a_rehearsal_record_ages_it_from_the_first_check_that_finds_none(
    watched: Host, uptime: float
) -> None:
    watched.uptime.write_text(f"{uptime:.2f} 0.00\n")
    watched.last_rehearsal.unlink()

    watchdog.main()

    assert rehearsal_events(watched) == []
    assert json.loads(watched.last_rehearsal.read_text()) == {
        "status": "scheduled",
        "mark": {"boot_id": "boot-a", "uptime_seconds": uptime},
    }
    assert [
        path.name
        for path in watched.last_rehearsal.parent.iterdir()
        if path.name.startswith(watched.last_rehearsal.name)
    ] == [watched.last_rehearsal.name]


def test_a_rehearsal_record_written_meanwhile_is_never_replaced_by_the_schedule(
    watched: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    watched.last_rehearsal.unlink()
    mark = host.clock_mark

    def rehearsed_meanwhile() -> dict[str, object]:
        watched.rehearsed("failed", age=60)
        return mark()

    monkeypatch.setattr(host, "clock_mark", rehearsed_meanwhile)

    watchdog.main()

    assert json.loads(watched.last_rehearsal.read_text())["status"] == "failed"
    assert [
        path.name
        for path in watched.last_rehearsal.parent.iterdir()
        if path.name.startswith(watched.last_rehearsal.name)
    ] == [watched.last_rehearsal.name]


@pytest.mark.parametrize(
    ("age", "boot", "overdue"),
    [
        (DAY, "boot-a", False),
        (7 * DAY, "boot-a", False),
        (9 * DAY, "boot-a", True),
        (DAY, "boot-z", True),
    ],
)
def test_a_scheduled_rehearsal_is_overdue_once_its_first_run_could_have_passed_long_ago(
    watched: Host, age: float, boot: str, overdue: bool
) -> None:
    watched.uptime.write_text(f"{9 * DAY + 60:.2f} 0.00\n")
    mark = {"boot_id": boot, "uptime_seconds": 9 * DAY + 60 - age}
    watched.last_rehearsal.write_text(json.dumps({"status": "scheduled", "mark": mark}))
    recorded = watched.last_rehearsal.read_bytes()

    watchdog.main()

    assert rehearsal_events(watched) == ([("WARNING", {"state": "overdue"})] if overdue else [])
    assert watched.last_rehearsal.read_bytes() == recorded


@pytest.mark.parametrize(("uptime", "overdue"), [(DAY, False), (9 * DAY, True)])
@pytest.mark.parametrize(
    "status",
    [
        "not json",
        json.dumps(["passed"]),
        json.dumps({"status": "passed", "mark": {"boot_id": "boot-z", "uptime_seconds": 1}}),
        json.dumps({"status": "unknown"}),
    ],
)
def test_without_a_rehearsal_of_this_boot_it_is_overdue_once_the_boot_outlasts_the_bound(
    watched: Host, status: str, uptime: float, overdue: bool
) -> None:
    watched.uptime.write_text(f"{uptime:.2f} 0.00\n")
    watched.last_rehearsal.write_text(status)

    watchdog.main()
    assert rehearsal_events(watched) == ([("WARNING", {"state": "overdue"})] if overdue else [])


@pytest.mark.parametrize(("remaining_days", "warns"), [(30, False), (22, True)])
def test_certificate_warning_when_less_than_a_quarter_of_its_lifetime_remains(
    watched: Host, monkeypatch: pytest.MonkeyPatch, remaining_days: int, warns: bool
) -> None:
    probes: list[tuple[str, tuple[str, int]]] = []

    def validity(server_name: str, address: tuple[str, int]) -> tuple[float, float]:
        probes.append((server_name, address))
        return remaining_days * DAY, 90 * DAY

    monkeypatch.setattr(watchdog, "certificate_validity", validity)
    watchdog.main()
    assert probes == [("example.com", ("127.0.0.1", host.LOCAL_HTTPS_PORT))]
    expected = [{"days_remaining": float(remaining_days)}] if warns else []
    assert watched.details("pyracast_certificate_expiring") == expected


def key_usage(*, signs_certificates: bool) -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=not signs_certificates,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=signs_certificates,
        crl_sign=signs_certificates,
        encipher_only=False,
        decipher_only=False,
    )


def test_certificate_validity_reads_the_served_certificate(tmp_path: Path) -> None:
    now = datetime.datetime.now(datetime.UTC).replace(microsecond=0)
    authority_key = ec.generate_private_key(ec.SECP256R1())
    authority_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Watchdog test CA")])
    authority = (
        x509.CertificateBuilder()
        .subject_name(authority_name)
        .issuer_name(authority_name)
        .public_key(authority_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=100))
        .not_valid_after(now + datetime.timedelta(days=100))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(key_usage(signs_certificates=True), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(authority_key.public_key()), critical=False
        )
        .sign(authority_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(authority_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=80))
        .not_valid_after(now + datetime.timedelta(days=10))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(key_usage(signs_certificates=False), critical=True)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(authority_key.public_key()),
            critical=False,
        )
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(authority_key, hashes.SHA256())
    )
    chain = tmp_path / "chain.pem"
    chain.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    private = tmp_path / "key.pem"
    private.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(chain, private)
    client = ssl.create_default_context(
        cadata=authority.public_bytes(serialization.Encoding.PEM).decode()
    )

    with socket.create_server(("127.0.0.1", 0)) as listener:

        def serve() -> None:
            connection, _ = listener.accept()
            try:
                with server.wrap_socket(connection, server_side=True) as secure:
                    secure.recv(1)
            except OSError:
                pass

        thread = threading.Thread(target=serve)
        thread.start()
        remaining, lifetime = watchdog.certificate_validity(
            "localhost", listener.getsockname()[:2], client
        )
        thread.join(timeout=10)

    assert lifetime == 90 * DAY
    assert abs(remaining - 10 * DAY) < 120


def test_no_command_starts_after_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", lambda command, **_options: started.append(command))
    run = watchdog.Run()
    run.expires = time.monotonic() - 1

    with pytest.raises(subprocess.TimeoutExpired):
        run.command(["docker", "compose", "ps"])
    assert started == []


def test_every_command_gets_only_the_time_left_before_the_shared_deadline(
    watched: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [1000.0]

    def slow(command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        result = watched.run(command, **options)
        clock[0] += 10
        return result

    monkeypatch.setattr(
        watchdog, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time)
    )
    monkeypatch.setattr(subprocess, "run", slow)
    watched.snapshots = [states(worker="exited"), states(worker="exited"), HEALTHY]

    assert watchdog.main() == 0
    assert len(watched.timeouts) > 8
    assert watched.timeouts == [
        watchdog.DEADLINE_SECONDS - 10 * index for index in range(len(watched.timeouts))
    ]
    ((index, start),) = [
        (index, command)
        for index, command in enumerate(watched.commands)
        if "--wait-timeout" in command
    ]
    assert int(start[start.index("--wait-timeout") + 1]) == watched.timeouts[index] - 10


@pytest.mark.parametrize(
    ("boot_free", "data_free", "memory_free", "warns"),
    [(10, 50, 50, True), (50, 10, 50, True), (50, 50, 5, True), (50, 50, 50, False)],
)
def test_capacity_warning_covers_boot_disk_data_disk_and_memory(
    watched: Host, boot_free: int, data_free: int, memory_free: int, warns: bool
) -> None:
    watched.free = {"/": boot_free, "/var/lib/pyracast": data_free}
    watched.meminfo.write_text(f"MemTotal: 100 kB\nMemAvailable: {memory_free} kB\n")
    watchdog.check_capacity()
    expected = (
        "pyracast_host_capacity",
        "WARNING",
        {
            "boot_disk_free_percent": float(boot_free),
            "data_disk_free_percent": float(data_free),
            "memory_available_percent": float(memory_free),
        },
    )
    assert watched.events == ([expected] if warns else [])


class Listing:
    def __init__(self, listed: str, containers: list[dict[str, object]]) -> None:
        self.listed = listed
        self.containers = containers
        self.commands: list[list[str]] = []

    def command(self, command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        output = (
            json.dumps(self.containers) if command[:2] == ["docker", "inspect"] else self.listed
        )
        return subprocess.CompletedProcess(command, 0, output, "")


def test_a_project_without_containers_reports_every_service_missing() -> None:
    listing = Listing("", [])

    assert watchdog.inspect_services(cast(watchdog.Run, listing), ["compose"]) == dict.fromkeys(
        watchdog.SERVICES, "missing"
    )
    assert listing.commands == [["compose", "ps", "--all", "--quiet"]]


def test_containers_of_services_the_watchdog_does_not_supervise_are_ignored() -> None:
    listing = Listing("one\ntwo\n", [container("api", "healthy"), container("migrate", "exited")])

    assert watchdog.inspect_services(cast(watchdog.Run, listing), ["compose"]) == {
        **dict.fromkeys(watchdog.SERVICES, "missing"),
        "api": "healthy",
    }
    assert listing.commands[-1] == ["docker", "inspect", "one", "two"]


@pytest.mark.parametrize(
    ("origin_pull", "observation", "reported"),
    [
        ("required", probe_origin_pull.ENFORCED, []),
        ("required", probe_origin_pull.CLOSED, [{"observation": probe_origin_pull.CLOSED}]),
        ("off", probe_origin_pull.CLOSED, []),
    ],
)
def test_origin_pull_enforcement_is_checked_between_releases(
    watched: Host,
    monkeypatch: pytest.MonkeyPatch,
    origin_pull: str,
    observation: str,
    reported: list[dict[str, object]],
) -> None:
    (watched.release / "deploy/release.env").write_text(
        f"SITE_ADDRESS=example.com\nORIGIN_PULL={origin_pull}\n"
    )
    probes: list[tuple[object, ...]] = []

    def observe(address: str, port: int, server_name: str, cafile: str | None, wait: float) -> str:
        probes.append((address, port, server_name, cafile, wait))
        return observation

    monkeypatch.setattr(probe_origin_pull, "observe", observe)

    assert watchdog.main() == 0
    assert watched.details("pyracast_origin_pull_unenforced") == reported
    expected = [("127.0.0.1", host.PUBLIC_HTTPS_PORT, "example.com", None, 5.0)]
    assert probes == (expected if origin_pull == "required" else [])
    assert watched.heartbeat() == {"state": "ok"}
