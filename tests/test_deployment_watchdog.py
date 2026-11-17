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
from typing import Any, NamedTuple

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from deploy import host, watchdog

HEALTHY = dict.fromkeys(watchdog.SERVICES, "running")
DAY = 86400


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
        self.last_backup = root / "last-backup.json"
        self.last_backup.write_text(json.dumps({"completed_at": time.time()}))
        self.delivery_checked = root / "delivery-checked"
        self.meminfo = root / "meminfo"
        self.meminfo.write_text("MemTotal: 100 kB\nMemAvailable: 50 kB\n")
        self.free = {"/": 50, "/var/lib/pyracast": 50}
        self.unit = "active"
        self.snapshots = [HEALTHY]
        self.delivery: int | BaseException = 0
        self.lock_free = True
        self.held = False
        self.on_lock: Callable[[], None] = lambda: None
        self.commands: list[list[str]] = []
        self.locked: list[list[str]] = []
        self.timeouts: list[float] = []
        self.events: list[tuple[str, str, dict[str, object]]] = []

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
            return subprocess.CompletedProcess(command, self.delivery, "{}", "")
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
    watched.last_backup.write_text(json.dumps({"completed_at": time.time() - 2 * 3600}))

    assert watchdog.main() == 1
    assert watched.names() == [
        "pyracast_host_capacity",
        "pyracast_backup_failed",
        "pyracast_runtime_unhealthy",
        "pyracast_watchdog_heartbeat",
    ]
    assert watched.details("pyracast_runtime_unhealthy") == [
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
    assert watched.details("pyracast_runtime_unhealthy") == [
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
    watched.last_backup.write_text(json.dumps({"completed_at": time.time() - 3 * 3600}))

    assert watchdog.main() == 0
    assert watched.commands == []
    assert watched.names() == ["pyracast_host_capacity", "pyracast_watchdog_heartbeat"]
    assert watched.heartbeat() == {"state": "blocked"}


@pytest.mark.parametrize("unit", ["inactive", "failed", "activating"])
def test_inactive_application_unit_is_reported_without_recovery(watched: Host, unit: str) -> None:
    watched.unit = unit
    watched.snapshots = [states(worker="exited")]
    watched.last_backup.write_text(json.dumps({"completed_at": time.time() - 3 * 3600}))

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
    assert ({"reason": "delivery_health"} in watched.details("pyracast_runtime_unhealthy")) is runs
    assert (watched.delivery_checked.stat().st_mtime > stamp + 1) is runs


def test_first_delivery_check_runs_and_records_its_time(watched: Host) -> None:
    watchdog.main()
    assert watched.compose("pyracast", "check_delivery_health")
    assert watched.delivery_checked.exists()
    assert "pyracast_runtime_unhealthy" not in watched.names()


@pytest.mark.parametrize(
    ("marker", "age", "stale"),
    [
        ({"completed_at": "recent"}, 0, False),
        ({"completed_at": "old"}, 0, True),
        ({"completed_at": "ahead"}, 0, True),
        ({"completed_at": "slightly ahead"}, 0, False),
        ({"completed_at": "recent"}, 3 * 3600, False),
        ("not json", 3 * 3600, True),
        ({"object": "postgres/x.dump"}, 0, False),
        (None, 0, False),
        (None, 3 * 3600, True),
    ],
)
def test_backup_age_uses_the_recorded_completion_time(
    watched: Host, marker: object, age: float, stale: bool
) -> None:
    now = time.time()
    completed = {
        "recent": now - 60,
        "old": now - 3 * 3600,
        "ahead": now + 3600,
        "slightly ahead": now + 60,
    }
    if marker is None:
        watched.last_backup.unlink()
        target = watched.release / "deploy/release.env"
    else:
        target = watched.last_backup
        if isinstance(marker, dict):
            marker = {key: completed.get(value, value) for key, value in marker.items()}
            target.write_text(json.dumps(marker))
        else:
            target.write_text(str(marker))
    os.utime(target, (now - age, now - age))

    watchdog.main()
    assert ("pyracast_backup_failed" in watched.names()) is stale


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
    assert probes == [("example.com", ("127.0.0.1", 443))]
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
