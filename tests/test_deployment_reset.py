import base64
import contextlib
import hashlib
import io
import json
import re
import runpy
import shlex
import signal
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from types import FrameType, SimpleNamespace
from typing import Any

import pytest
from django.db import connection

from deploy import reset_database
from deploy.reset_database import Host, RefusedError

ROOT = Path(__file__).resolve().parents[1]
REVISION = "a" * 40
BACKEND_IMAGE = "registry.example/backend@sha256:" + "1" * 64
SUPPORT = {"postgres", "rabbitmq"}
APPLICATION = {"gateway", "api", "web", "dispatcher", "worker", "notifications"}
FOREIGN_OBJECT = "postgres/2026/11/16/20261116T220000Z-prerelease-0123456789abcdef.dump"


class FakeCommands:
    """Answers the Docker, PostgreSQL, RabbitMQ and curl commands of one host.

    A dump carries the live row counts as JSON, so a restore reproduces them.
    """

    def __init__(self, environment: Path) -> None:
        self.environment = environment
        self.calls: list[list[str]] = []
        self.inputs: list[str] = []
        self.unservable = False
        self.running = APPLICATION | SUPPORT
        self.stuck: set[str] = set()
        self.live: dict[str, int] = {"accounts_tenant": 2, "django_migrations": 88}
        self.throwaway: dict[str, int] = {}
        self.verification_counts: dict[str, int] | None = None
        self.restore_drops_a_table = False
        self.queues = {"default": 3, "default.DQ": 1, "notifications.XQ": 2}
        self.queues_refill = False
        self.ready = True
        self.roles = "2"
        self.fail: str | None = None
        self.lock_held = False
        self.locks = SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=self.flock)

    def flock(self, _handle: object, _operation: int) -> None:
        if self.lock_held:
            raise BlockingIOError

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        script = kwargs.get("input")
        self.calls.append([*command, script] if isinstance(script, str) and script else [*command])
        if isinstance(script, str):
            self.inputs.append(script)
        if self.fail is not None and self.fail in " ".join(command):
            raise subprocess.CalledProcessError(1, command, "", "fixture failure detail")
        if self.needs_missing_environment(command):
            # Compose resolves required env_file entries for config, up and run, not for ps or exec.
            if kwargs.get("check"):
                raise subprocess.CalledProcessError(1, command, "", "env file not found")
            return subprocess.CompletedProcess(command, 1, "", "env file not found")
        if command[0] == "curl":
            return subprocess.CompletedProcess(
                command, 0 if self.ready else 22, "ok\n" if self.ready else "", ""
            )
        if "migrate" in command and "--check" in command:
            applied = bool(self.live.get("django_migrations")) and not self.unservable
            return subprocess.CompletedProcess(command, 0 if applied else 1, "", "")
        stdout = ""
        if command[1].endswith("runtime_secrets.py"):
            (self.environment / "runtime.env").write_text("PGUSER=pyracast_app\n")
        elif "ps" in command:
            stdout = "".join(f"{service}\n" for service in sorted(self.running))
        elif "stop" in command:
            self.running -= set(command[command.index("stop") + 3 :]) - self.stuck
        elif "up" in command:
            named = set(command[command.index("--wait-timeout") + 2 :])
            self.running |= named or APPLICATION | SUPPORT
        elif "pg_dump" in command:
            kwargs["stdout"].write(json.dumps(self.live).encode())
        elif "pg_restore" in command:
            restored: dict[str, int] = json.loads(kwargs["stdin"].read())
            if "--no-owner" in command:
                self.throwaway = restored
            else:
                if self.restore_drops_a_table:
                    restored.popitem()
                self.live = restored
        elif "psql" in command:
            stdout = self.psql(command)
        elif "rabbitmqctl" in command:
            stdout = self.rabbitmq(command)
        elif "config" in command:
            stdout = json.dumps({"services": {"postgres": {"image": "postgres@sha256:fixture"}}})
        return subprocess.CompletedProcess(command, 0, stdout, "")

    def needs_missing_environment(self, command: list[str]) -> bool:
        return (
            command[:2] == ["docker", "compose"]
            and bool({"config", "up", "run"} & set(command))
            and not (self.environment / "runtime.env").is_file()
        )

    def psql(self, command: list[str]) -> str:
        if reset_database.COUNT_SQL in command:
            if command[:2] == ["docker", "exec"]:
                counts = self.verification_counts or self.throwaway
            else:
                counts = self.live
            return "".join(f"{name} {count}\n" for name, count in sorted(counts.items()))
        if any("pg_roles" in argument for argument in command):
            return self.roles + "\n"
        if "DROP DATABASE IF EXISTS pyracast WITH (FORCE)" in command:
            self.live = {}
        return ""

    def rabbitmq(self, command: list[str]) -> str:
        if "purge_queue" in command and not self.queues_refill:
            self.queues[command[-1]] = 0
        return "".join(f"{name}\t{count}\n" for name, count in sorted(self.queues.items()))

    def index(self, predicate: str) -> int:
        return next(
            position for position, call in enumerate(self.calls) if predicate in " ".join(call)
        )

    def destructive_calls(self) -> list[list[str]]:
        return [
            call
            for call in self.calls
            if "purge_queue" in call
            or "DROP DATABASE IF EXISTS pyracast WITH (FORCE)" in call
            or ("pg_restore" in call and "--no-owner" not in call)
        ]


@pytest.fixture
def host(tmp_path: Path) -> Host:
    release = tmp_path / "releases" / REVISION
    (release / "deploy").mkdir(parents=True)
    (release / "deploy/release.env").write_text(
        f"BACKEND_IMAGE={BACKEND_IMAGE}\nSITE_ADDRESS=example.com\n"
    )
    (release / "deploy/bootstrap.sql").write_text((ROOT / "deploy/bootstrap.sql").read_text())
    environment = tmp_path / "run"
    environment.mkdir()
    (environment / "runtime.env").write_text("PGUSER=pyracast_app\n")
    return Host(
        config=tmp_path / "host.json",
        release=release,
        bucket="private-backups",
        runtime_version="projects/example/secrets/runtime/versions/7",
        marker=tmp_path / "lib/release-blocked",
        lock=tmp_path / "lock/release.lock",
        state=tmp_path / "lib/reset",
        environment=environment,
    )


@pytest.fixture
def commands(host: Host, monkeypatch: pytest.MonkeyPatch) -> FakeCommands:
    fake = FakeCommands(host.environment)
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        reset_database, "importlib", SimpleNamespace(import_module=lambda _name: fake.locks)
    )
    return fake


@pytest.fixture
def uploads(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    stored: dict[str, bytes] = {}

    def upload(bucket: str, name: str, archive: Path) -> None:
        assert bucket == "private-backups"
        stored[name] = archive.read_bytes()

    monkeypatch.setattr(reset_database, "upload_backup", upload)
    return stored


def prepared(host: Host, uploads: dict[str, bytes]) -> str:
    reset_database.prepare(host)
    return next(iter(uploads))


def kept_archive(host: Host, name: str) -> Path:
    return host.state / name.rsplit("/", 1)[-1]


START_UNIT = [
    ["systemctl", "reset-failed", "pyracast.service"],
    ["systemctl", "start", "pyracast.service"],
]


def secrets_call(host: Host) -> list[str]:
    return [sys.executable, str(host.release / "deploy/runtime_secrets.py"), str(host.config)]


def full_start(commands: FakeCommands) -> int:
    return next(
        position
        for position, call in enumerate(commands.calls)
        if "up" in call and call[-1] == "180"
    )


def finish_release(commands: FakeCommands, host: Host) -> None:
    """What a successful install does: it migrates, starts and removes the marker."""
    commands.live = {"accounts_tenant": 1, "django_migrations": 89}
    commands.running = APPLICATION | SUPPORT
    host.marker.unlink()


def fail_release(commands: FakeCommands, host: Host) -> None:
    """What a later install that fails in maintenance leaves: a new marker, stopped services."""
    host.marker.touch()
    commands.running = set(SUPPORT)


def test_prepare_blocks_drains_dumps_uploads_verifies_and_keeps_the_archive(
    host: Host,
    commands: FakeCommands,
    uploads: dict[str, bytes],
    capsys: pytest.CaptureFixture[str],
) -> None:
    name = prepared(host, uploads)
    output = capsys.readouterr().out
    assert re.fullmatch(r"postgres/\d{4}/\d\d/\d\d/\d{8}T\d{6}Z-prereset-[0-9a-f]{32}\.dump", name)
    archive = kept_archive(host, name)
    assert archive.read_bytes() == uploads[name]
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert host.marker.exists()
    assert commands.running == SUPPORT
    stops = [call[call.index("stop") + 1 :] for call in commands.calls if "stop" in call]
    assert stops == [
        ["-t", "100", "gateway", "api", "web"],
        ["-t", "660", "dispatcher"],
        ["-t", "660", "worker", "notifications"],
    ]
    assert commands.calls.index(["sync", "-f", str(host.marker)]) < commands.index(" stop ")
    assert commands.index(" stop -t 660 worker") < commands.index("pg_dump")
    listed = commands.calls[commands.index(" ps --services")]
    assert listed[-6:] == ["--status", "running", "--status", "restarting", "--status", "paused"]
    probe = commands.calls[0]
    assert "--resolve" in probe
    assert "example.com:443:127.0.0.1" in probe
    assert probe[-1] == "https://example.com/health/ready/"
    for expected in (
        f"gs://private-backups/{name}",
        digest,
        str(archive),
        REVISION,
        f"BACKEND_IMAGE={BACKEND_IMAGE}",
        "projects/example/secrets/runtime/versions/7",
        "accounts_tenant 2",
        "django_migrations 88",
        f"reset_database.py {host.config} reset --confirm {name}",
        f"reset_database.py {host.config} abort",
    ):
        assert expected in output


def test_prepare_refuses_while_a_release_is_blocked(
    host: Host, commands: FakeCommands, uploads: dict[str, bytes]
) -> None:
    host.marker.parent.mkdir(parents=True)
    host.marker.touch()
    expected = f"release-blocked exists.* {re.escape(str(host.config))} abort;"
    with pytest.raises(RefusedError, match=expected):
        reset_database.prepare(host)
    assert not any("stop" in call for call in commands.calls)
    assert uploads == {}


def test_prepare_refused_after_a_reset_names_the_restore_of_its_archive(
    host: Host, commands: FakeCommands, uploads: dict[str, bytes]
) -> None:
    name = prepared(host, uploads)
    reset_database.reset(host, name)
    with pytest.raises(RefusedError, match=f"run .* restore --confirm {name};"):
        reset_database.prepare(host)
    assert len(uploads) == 1


def test_prepare_refuses_when_the_site_is_not_ready(
    host: Host, commands: FakeCommands, uploads: dict[str, bytes]
) -> None:
    commands.ready = False
    with pytest.raises(RefusedError, match="health/ready"):
        reset_database.prepare(host)
    assert not host.marker.exists()
    assert commands.running == APPLICATION | SUPPORT


@pytest.mark.parametrize("subcommand", ["prepare", "abort", "reset", "restore"])
def test_every_subcommand_refuses_while_another_operation_holds_the_release_lock(
    host: Host, commands: FakeCommands, uploads: dict[str, bytes], subcommand: str
) -> None:
    commands.lock_held = True
    host.marker.parent.mkdir(parents=True)
    if subcommand != "prepare":
        host.marker.touch()
    run: Callable[[], None] = {
        "prepare": lambda: reset_database.prepare(host),
        "abort": lambda: reset_database.abort(host),
        "reset": lambda: reset_database.reset(host, FOREIGN_OBJECT),
        "restore": lambda: reset_database.restore(host, FOREIGN_OBJECT),
    }[subcommand]
    with pytest.raises(RefusedError, match="release lock"):
        run()
    assert commands.calls == []
    assert host.marker.exists() == (subcommand != "prepare")


def test_prepare_dumps_nothing_while_an_application_container_keeps_running(
    host: Host, commands: FakeCommands, uploads: dict[str, bytes]
) -> None:
    commands.stuck = {"worker"}
    with pytest.raises(RuntimeError, match="still run: worker"):
        reset_database.prepare(host)
    assert host.marker.exists()
    assert not any("pg_dump" in call for call in commands.calls)
    assert uploads == {}


def test_prepare_records_nothing_when_the_verification_restore_differs(
    host: Host, commands: FakeCommands, uploads: dict[str, bytes]
) -> None:
    commands.verification_counts = {"accounts_tenant": 1, "django_migrations": 88}
    with pytest.raises(RuntimeError, match="accounts_tenant: expected 2, found 1"):
        reset_database.prepare(host)
    assert host.marker.exists()
    name = next(iter(uploads))
    with pytest.raises(RefusedError, match="not the archive that prepare verified"):
        reset_database.reset(host, name)
    assert commands.destructive_calls() == []


def test_failed_verification_restore_removes_its_container_and_volume(
    host: Host, commands: FakeCommands, uploads: dict[str, bytes]
) -> None:
    commands.fail = "--no-owner"
    with pytest.raises(subprocess.CalledProcessError):
        reset_database.prepare(host)
    assert commands.calls[-2][:3] == ["docker", "rm", "--force"]
    assert commands.calls[-1][:3] == ["docker", "volume", "rm"]
    start = next(call for call in commands.calls if call[:2] == ["docker", "run"])
    assert start[start.index("--network") + 1] == "none"


def test_verification_restore_waits_for_the_tcp_server(
    host: Host, commands: FakeCommands, uploads: dict[str, bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    probes = 0
    answer = commands.__call__

    def slow_start(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        nonlocal probes
        if "pg_isready" in command:
            probes += 1
            return subprocess.CompletedProcess(command, 2 if probes < 3 else 0)
        return answer(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", slow_start)
    reset_database.prepare(host)
    assert probes == 3


def test_verification_restore_gives_up_when_the_throwaway_server_never_starts(
    host: Host, commands: FakeCommands, uploads: dict[str, bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = commands.__call__

    def never_ready(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if "pg_isready" in command:
            return subprocess.CompletedProcess(command, 2)
        return answer(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", never_ready)
    with pytest.raises(RuntimeError, match="did not become ready"):
        reset_database.prepare(host)


def test_abort_starts_the_previous_release_and_clears_the_marker(
    host: Host,
    commands: FakeCommands,
    uploads: dict[str, bytes],
    capsys: pytest.CaptureFixture[str],
) -> None:
    prepared(host, uploads)
    reset_database.abort(host)
    assert not host.marker.exists()
    assert commands.running == APPLICATION | SUPPORT
    # The previous release renders its own secrets: the failed release may have changed them.
    secrets = commands.calls.index(secrets_call(host))
    check = commands.index("api pyracast migrate --check")
    assert secrets < check < full_start(commands)
    assert commands.calls[-3][:3] == ["sync", "-f", str(host.marker.parent)]
    assert "--retry" in commands.calls[-4]
    assert commands.calls[-2:] == START_UNIT
    assert not host.session.exists()
    assert "serves again" in capsys.readouterr().out


def test_abort_stops_the_application_again_and_keeps_the_marker_when_readiness_fails(
    host: Host, commands: FakeCommands, uploads: dict[str, bytes]
) -> None:
    prepared(host, uploads)
    commands.ready = False
    with pytest.raises(RuntimeError, match="did not answer ok"):
        reset_database.abort(host)
    assert host.marker.exists()
    assert commands.running == SUPPORT
    commands.ready = True
    reset_database.abort(host)
    assert not host.marker.exists()


def test_abort_refuses_when_the_release_cannot_serve_the_database(
    host: Host, commands: FakeCommands, uploads: dict[str, bytes]
) -> None:
    host.marker.parent.mkdir(parents=True)
    host.marker.touch()
    commands.running = set(SUPPORT)
    commands.live = {}
    with pytest.raises(RefusedError, match="cannot serve database pyracast"):
        reset_database.abort(host)
    assert not any("up" in call and call[-1] == "180" for call in commands.calls)
    assert host.marker.exists()


def test_abort_requires_an_active_maintenance_marker(host: Host, commands: FakeCommands) -> None:
    with pytest.raises(RefusedError, match="only during maintenance"):
        reset_database.abort(host)
    assert commands.calls == []


@pytest.mark.parametrize("operation", ["abort", "restore"])
@pytest.mark.parametrize("failure", ["systemctl reset-failed", "systemctl start"])
def test_service_unit_failure_preserves_maintenance_for_retry(
    host: Host,
    commands: FakeCommands,
    uploads: dict[str, bytes],
    operation: str,
    failure: str,
) -> None:
    name = prepared(host, uploads)
    if operation == "restore":
        reset_database.reset(host, name)
    identity = host.marker.read_text()
    commands.fail = failure

    def action() -> None:
        if operation == "abort":
            reset_database.abort(host)
        else:
            reset_database.restore(host, name)

    with pytest.raises(reset_database.StartFailedError) as failed:
        action()
    assert isinstance(failed.value.failure, subprocess.CalledProcessError)
    assert host.marker.read_text() == identity
    assert reset_database.current_session(host) is not None
    assert commands.running == SUPPORT
    commands.fail = None
    action()
    assert not host.marker.exists()
    assert not host.session.exists()
    assert commands.running == APPLICATION | SUPPORT


def test_abort_refuses_once_the_database_was_replaced(
    host: Host, commands: FakeCommands, uploads: dict[str, bytes]
) -> None:
    name = prepared(host, uploads)
    reset_database.reset(host, name)
    calls = len(commands.calls)
    with pytest.raises(RefusedError, match=f"restore --confirm {name}"):
        reset_database.abort(host)
    assert commands.calls[calls:] == []
    assert host.marker.exists()


def test_abort_stays_refused_when_the_drop_fails(
    host: Host, commands: FakeCommands, uploads: dict[str, bytes]
) -> None:
    name = prepared(host, uploads)
    commands.fail = "DROP DATABASE"
    with pytest.raises(subprocess.CalledProcessError):
        reset_database.reset(host, name)
    assert commands.calls.index(["sync", "-f", str(host.session)]) < commands.index("DROP")
    commands.fail = None
    with pytest.raises(RefusedError, match=f"restore --confirm {name}"):
        reset_database.abort(host)


def test_abort_stays_refused_when_restored_counts_differ(
    host: Host, commands: FakeCommands, uploads: dict[str, bytes]
) -> None:
    name = prepared(host, uploads)
    reset_database.reset(host, name)
    commands.restore_drops_a_table = True
    with pytest.raises(RuntimeError, match="row counts differ"):
        reset_database.restore(host, name)
    with pytest.raises(RefusedError, match=f"restore --confirm {name}"):
        reset_database.abort(host)
    assert commands.running == SUPPORT


def test_abort_serves_the_new_database_after_a_successful_reset_release(
    host: Host,
    commands: FakeCommands,
    uploads: dict[str, bytes],
    capsys: pytest.CaptureFixture[str],
) -> None:
    name = prepared(host, uploads)
    reset_database.reset(host, name)
    finish_release(commands, host)
    fail_release(commands, host)
    reset_database.abort(host)
    assert commands.live == {"accounts_tenant": 1, "django_migrations": 89}
    assert commands.running == APPLICATION | SUPPORT
    assert not host.marker.exists()
    assert "restore --confirm" not in capsys.readouterr().err
    reset_database.prepare(host)
    earlier = f"Earlier archives in {host.state}: {kept_archive(host, name).name}."
    assert earlier in capsys.readouterr().out
    with pytest.raises(RefusedError, match="not the archive that prepare verified"):
        reset_database.reset(host, name)
    assert commands.live == {"accounts_tenant": 1, "django_migrations": 89}


def test_abort_writes_runtime_secrets_and_rearms_the_unit_after_a_host_restart(
    host: Host, commands: FakeCommands, uploads: dict[str, bytes]
) -> None:
    prepared(host, uploads)
    (host.environment / "runtime.env").unlink()
    reset_database.abort(host)
    secrets = commands.calls.index(secrets_call(host))
    assert secrets < commands.index("up --detach --wait --wait-timeout 180")
    assert commands.calls[-2:] == START_UNIT
    assert not host.marker.exists()


def test_reset_and_restore_write_the_runtime_secrets_after_a_host_restart(
    host: Host, commands: FakeCommands, uploads: dict[str, bytes]
) -> None:
    name = prepared(host, uploads)
    (host.environment / "runtime.env").unlink()
    reset_database.reset(host, name)
    first = len(commands.calls)
    assert commands.calls.index(secrets_call(host)) < commands.index(" up --detach")
    fail_release(commands, host)
    reset_database.restore(host, name)
    restored = commands.calls[first:]
    assert restored.index(secrets_call(host)) < next(
        position for position, call in enumerate(restored) if "up" in call
    )
    assert commands.calls[-2:] == START_UNIT
    assert not host.marker.exists()


def test_a_retried_abort_after_a_host_restart_starts_the_unit(
    host: Host, commands: FakeCommands, uploads: dict[str, bytes]
) -> None:
    prepared(host, uploads)
    (host.environment / "runtime.env").unlink()
    commands.ready = False
    with pytest.raises(RuntimeError, match="did not answer ok"):
        reset_database.abort(host)
    assert not any("systemctl" in call for call in commands.calls)
    commands.ready = True
    reset_database.abort(host)
    assert commands.calls[-2:] == START_UNIT
    assert not host.marker.exists()


@pytest.mark.django_db
@pytest.mark.parametrize(("role", "readable"), [("pg_read_all_data", True), ("pg_monitor", False)])
def test_runtime_table_privilege_check_uses_each_tables_schema(role: str, readable: bool) -> None:
    query = re.search(
        r"SELECT FROM pg_tables WHERE schemaname = 'public'\s+"
        r"AND NOT has_table_privilege\(.+?'SELECT'\)",
        reset_database.ROLE_CHECK_SQL,
        re.DOTALL,
    )
    assert query is not None
    statement = query[0].replace("%", "%%").replace("'pyracast_app'", "%s")
    with connection.cursor() as cursor:
        cursor.execute(statement, [role])
        missing = cursor.fetchall()
    assert (missing == []) is readable


def test_reset_purges_every_queue_recreates_the_database_and_checks_both_roles(
    host: Host,
    commands: FakeCommands,
    uploads: dict[str, bytes],
    capsys: pytest.CaptureFixture[str],
) -> None:
    name = prepared(host, uploads)
    reset_database.reset(host, name)
    assert commands.queues == {"default": 0, "default.DQ": 0, "notifications.XQ": 0}
    purged = [call[-1] for call in commands.calls if "purge_queue" in call]
    assert purged == ["default", "default.DQ", "notifications.XQ"]
    recreate = commands.calls[commands.index("DROP DATABASE")]
    assert recreate[recreate.index("-d") + 1] == "postgres"
    assert recreate[-4:] == [
        "-c",
        "DROP DATABASE IF EXISTS pyracast WITH (FORCE)",
        "-c",
        "CREATE DATABASE pyracast",
    ]
    assert commands.index("purge_queue") < commands.index("DROP DATABASE")
    bootstrap = next(script for script in commands.inputs if "REVOKE ALL ON DATABASE" in script)
    assert "CREATE ROLE" not in bootstrap
    assert reset_database.ROLE_CHECK_SQL in commands.inputs
    connect = commands.calls[commands.index("run --rm --no-deps api")]
    assert connect[-1] == reset_database.CONNECT_CHECK
    assert commands.live == {}
    assert host.marker.exists()
    output = capsys.readouterr().out
    assert f"reset_database.py {host.config} restore --confirm {name}" in output
    assert "Release" in output
    reset_database.reset(host, name)
    assert commands.calls.count(recreate) == 2


@pytest.mark.parametrize(
    "condition",
    [
        "unknown-object",
        "invalid-object",
        "missing-archive",
        "changed-archive",
        "no-marker",
        "application-running",
        "missing-roles",
        "same-basename-elsewhere",
        "earlier-maintenance",
        "record-without-prepare",
        "missing-record",
    ],
)
def test_reset_refuses_before_any_destructive_step(
    host: Host, commands: FakeCommands, uploads: dict[str, bytes], condition: str
) -> None:
    name = prepared(host, uploads)
    match condition:
        case "unknown-object":
            name = FOREIGN_OBJECT
        case "same-basename-elsewhere":
            name = "postgres/elsewhere/" + kept_archive(host, name).name
        case "earlier-maintenance":
            reset_database.abort(host)
            fail_release(commands, host)
        case "record-without-prepare":
            host.session.unlink()
        case "missing-record":
            kept_archive(host, name).with_suffix(".json").unlink()
        case "invalid-object":
            name = "backups/database.dump"
        case "missing-archive":
            kept_archive(host, name).unlink()
        case "changed-archive":
            kept_archive(host, name).write_bytes(b"tampered")
        case "no-marker":
            host.marker.unlink()
        case "application-running":
            commands.running.add("worker")
        case "missing-roles":
            commands.roles = "1"
    with pytest.raises(RefusedError):
        reset_database.reset(host, name)
    assert commands.destructive_calls() == []


def test_reset_stops_before_dropping_when_a_queue_keeps_messages(
    host: Host, commands: FakeCommands, uploads: dict[str, bytes]
) -> None:
    name = prepared(host, uploads)
    commands.queues_refill = True
    with pytest.raises(RuntimeError, match="default: 3"):
        reset_database.reset(host, name)
    assert not any("DROP DATABASE" in " ".join(call) for call in commands.calls)


def test_restore_brings_back_the_kept_archive_with_owners_and_starts_the_previous_release(
    host: Host,
    commands: FakeCommands,
    uploads: dict[str, bytes],
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = dict(commands.live)
    name = prepared(host, uploads)
    reset_database.reset(host, name)
    commands.queues["default"] = 5
    reset_database.restore(host, name)
    assert commands.live == before
    assert commands.queues["default"] == 0
    restore = next(
        call for call in commands.calls if "pg_restore" in call and "--no-owner" not in call
    )
    assert "--no-acl" not in restore
    assert {"--exit-on-error", "--single-transaction"} <= set(restore)
    restored = commands.calls.index(restore)
    assert commands.index("DROP DATABASE") < restored
    later = [" ".join(call) for call in commands.calls[restored:]]
    order = [
        next(position for position, call in enumerate(later) if predicate in call)
        for predicate in (
            "pyracast_reset_probe",
            reset_database.CONNECT_CHECK,
            "api pyracast migrate --check",
            "up --detach --wait --wait-timeout 180",
        )
    ]
    assert order == sorted(order)
    assert order[0] > 0
    assert not host.marker.exists()
    assert not host.session.exists()
    assert commands.running == APPLICATION | SUPPORT
    assert "serves again" in capsys.readouterr().out
    calls = len(commands.calls)
    with pytest.raises(RefusedError, match="only during maintenance"):
        reset_database.abort(host)
    assert len(commands.calls) == calls


def test_restore_keeps_the_site_stopped_when_restored_counts_differ(
    host: Host, commands: FakeCommands, uploads: dict[str, bytes]
) -> None:
    name = prepared(host, uploads)
    commands.restore_drops_a_table = True
    with pytest.raises(RuntimeError, match="django_migrations: expected 88, found None"):
        reset_database.restore(host, name)
    assert host.marker.exists()
    assert commands.running == SUPPORT


def test_restore_refuses_another_object_once_reset_replaced_the_database(
    host: Host, commands: FakeCommands, uploads: dict[str, bytes]
) -> None:
    name = prepared(host, uploads)
    reset_database.reset(host, name)
    calls = len(commands.calls)
    with pytest.raises(RefusedError, match=f"only {name} holds its data"):
        reset_database.restore(host, FOREIGN_OBJECT)
    assert commands.destructive_calls()[-1][-1] == "CREATE DATABASE pyracast"
    assert not any("pg_restore" in call for call in commands.calls[calls:])


def test_restore_refuses_a_kept_archive_recorded_for_another_object(
    host: Host, commands: FakeCommands, uploads: dict[str, bytes]
) -> None:
    name = prepared(host, uploads)
    other = "postgres/elsewhere/" + kept_archive(host, name).name
    with pytest.raises(RefusedError, match=f"is {name}, not {other}"):
        reset_database.restore(host, other)
    assert commands.destructive_calls() == []


def test_restore_refuses_an_archive_without_applied_migrations(
    host: Host, commands: FakeCommands, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_bucket(monkeypatch, json.dumps({}).encode())
    host.marker.parent.mkdir(parents=True)
    fail_release(commands, host)
    with pytest.raises(RefusedError, match="holds no applied migration"):
        reset_database.restore(host, FOREIGN_OBJECT)
    assert commands.destructive_calls() == []
    assert host.marker.exists()


def test_restore_keeps_the_site_in_maintenance_when_the_release_cannot_serve_the_archive(
    host: Host, commands: FakeCommands, uploads: dict[str, bytes]
) -> None:
    name = prepared(host, uploads)
    reset_database.reset(host, name)
    commands.unservable = True
    with pytest.raises(RuntimeError, match="cannot serve the restored database"):
        reset_database.restore(host, name)
    assert host.marker.exists()
    assert commands.running == SUPPORT
    with pytest.raises(RefusedError, match=f"restore --confirm {name}"):
        reset_database.abort(host)


def fake_bucket(monkeypatch: pytest.MonkeyPatch, content: bytes, *, md5: str | None = None) -> None:
    digest = hashlib.md5(content, usedforsecurity=False).digest()
    checksum = md5 or base64.b64encode(digest).decode()

    def urlopen(request: urllib.request.Request, **_kwargs: object) -> io.BytesIO:
        assert request.get_header("Authorization") == "Bearer private-token"
        assert "/b/private-backups/o/postgres%2F2026%2F11%2F16%2F" in request.full_url
        if request.full_url.endswith("?alt=media"):
            return io.BytesIO(content)
        return io.BytesIO(json.dumps({"md5Hash": checksum, "size": str(len(content))}).encode())

    monkeypatch.setattr(reset_database, "access_token", lambda: "private-token")
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)


def test_restore_downloads_and_verifies_an_archive_this_host_did_not_prepare(
    host: Host, commands: FakeCommands, monkeypatch: pytest.MonkeyPatch
) -> None:
    archived = {"accounts_tenant": 4, "django_migrations": 87}
    fake_bucket(monkeypatch, json.dumps(archived).encode())
    host.marker.parent.mkdir(parents=True)
    host.marker.touch()
    commands.running = set(SUPPORT)
    reset_database.restore(host, FOREIGN_OBJECT)
    assert commands.throwaway == archived
    assert commands.live == archived
    assert kept_archive(host, FOREIGN_OBJECT).is_file()
    assert not host.marker.exists()


def test_restore_refuses_a_download_that_differs_from_the_bucket_checksum(
    host: Host, commands: FakeCommands, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_bucket(monkeypatch, b"{}", md5="invalid")
    host.marker.parent.mkdir(parents=True)
    host.marker.touch()
    commands.running = set(SUPPORT)
    with pytest.raises(RefusedError, match="checksum"):
        reset_database.restore(host, FOREIGN_OBJECT)
    assert not kept_archive(host, FOREIGN_OBJECT).exists()
    assert commands.destructive_calls() == []
    assert host.marker.exists()


def test_database_bootstrap_keeps_only_the_statements_inside_the_database() -> None:
    source = (ROOT / "deploy/bootstrap.sql").read_text()
    statements = reset_database.database_bootstrap(source).splitlines()
    assert statements == [
        line
        for line in source.splitlines()
        if not line.startswith(("CREATE ROLE ", "GRANT pyracast_owner TO "))
    ]
    assert statements[1] == "BEGIN;"
    assert statements[-1] == "COMMIT;"
    assert len(source.splitlines()) - len(statements) == 3


def test_host_configuration_selects_the_production_paths(tmp_path: Path) -> None:
    config = tmp_path / "host.json"
    config.write_text(
        json.dumps({"backup_bucket": "private-backups", "runtime_secret_version": "versions/7"})
    )
    host = reset_database.load_host(config)
    assert host.release == Path("/opt/pyracast/current")
    assert host.marker == Path("/var/lib/pyracast/release-blocked")
    assert host.state == Path("/var/lib/pyracast/reset")
    config.write_text(json.dumps({"backup_bucket": "Invalid Bucket"}))
    with pytest.raises(ValueError, match="bucket"):
        reset_database.load_host(config)
    assert reset_database.main([str(config), "abort"]) == 2
    assert reset_database.main([str(tmp_path / "missing.json"), "abort"]) == 2


def test_command_line_reports_refusals_failures_and_the_next_step(
    host: Host,
    commands: FakeCommands,
    uploads: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(reset_database, "load_host", lambda _path: host)
    config = str(host.config)
    with pytest.raises(SystemExit) as missing:
        reset_database.main([config, "reset"])
    assert missing.value.code == 2
    capsys.readouterr()
    commands.fail = "pg_dump"
    assert reset_database.main([config, "prepare"]) == 1
    error = capsys.readouterr().err
    assert "prepare failed" in error
    assert "fixture failure detail" in error
    assert f"reset_database.py {config} abort" in error
    assert reset_database.main([config, "prepare"]) == 2
    assert "Refused:" in capsys.readouterr().err
    commands.fail = None
    assert reset_database.main([config, "abort"]) == 0
    assert reset_database.main([config, "prepare"]) == 0
    name = next(iter(uploads))
    assert reset_database.main([config, "reset", "--confirm", name]) == 0
    assert reset_database.main([config, "restore", "--confirm", name]) == 0
    assert not host.marker.exists()
    capsys.readouterr()
    host.marker.touch()
    commands.running = set(SUPPORT)
    commands.ready = False
    assert reset_database.main([config, "restore", "--confirm", name]) == 1
    error = capsys.readouterr().err
    assert "restore failed: The release started, but /health/ready/ did not answer ok." in error
    assert f"reset_database.py {config} restore --confirm {name} again" in error
    assert commands.running == SUPPORT
    commands.ready = True
    assert reset_database.main([config, "restore", "--confirm", name]) == 0
    assert not host.marker.exists()


FAULT_POINTS: dict[str, Callable[[list[str], Host], bool]] = {
    "start": lambda call, _host: "up" in call and call[-1] == "180",
    "readiness": lambda call, _host: call[0] == "curl",
    "unit": lambda call, _host: call[:2] == ["systemctl", "start"],
    "marker sync": lambda call, host: call == ["sync", "-f", str(host.marker)],
    "shutdown": lambda call, _host: "stop" in call,
    "status": lambda call, _host: "ps" in call,
}


def inject_faults(
    monkeypatch: pytest.MonkeyPatch, host: Host, commands: FakeCommands, faults: dict[str, str]
) -> list[list[str]]:
    """Arms each fault at its point once the release start begins; returns every command tried.

    "error" fails before the command or file operation runs, "interrupt", "failure" (non-zero
    exit with captured stderr) and "timeout" (killed at its timeout with captured stderr bytes, not
    valid UTF-8) after the command ran, and "stuck" lets the worker survive its stop.
    """
    attempted: list[list[str]] = []
    started = [False]

    def kind(point: str) -> str | None:
        return faults.get(point) if started[0] else None

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        attempted.append(command)
        started[0] = started[0] or FAULT_POINTS["start"](command, host)
        point = next((name for name, hit in FAULT_POINTS.items() if hit(command, host)), "")
        fault = kind(point)
        if fault == "error":
            raise OSError(5, "Input/output error")
        if fault == "stuck":
            commands.stuck = {"worker"}
        result = commands(command, **kwargs)
        if fault == "interrupt":
            raise KeyboardInterrupt
        if fault == "failure":
            raise subprocess.CalledProcessError(1, command, "", f"{point} failure detail")
        if fault == "timeout":
            detail = f"{point} failure detail ".encode() + b"\xff"
            raise subprocess.TimeoutExpired(command, kwargs["timeout"], b"", detail)
        return result

    def file_fault(point: str) -> None:
        if kind(point) == "error":
            raise OSError(5, "Input/output error")
        if kind(point) == "interrupt":
            raise KeyboardInterrupt

    write_text, read_text = Path.write_text, Path.read_text

    def guarded_write(path: Path, data: str, *args: Any, **kwargs: Any) -> int:
        if path == host.marker.with_name(host.marker.name + ".next"):
            file_fault("marker write")
        return write_text(path, data, *args, **kwargs)

    def guarded_read(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == host.marker:
            file_fault("marker read")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(Path, "write_text", guarded_write)
    monkeypatch.setattr(Path, "read_text", guarded_read)
    return attempted


@pytest.mark.parametrize(
    ("operation", "faults", "marker", "running", "code"),
    [
        pytest.param("abort", {"start": "error"}, "held", set(), 1, id="start-error"),
        pytest.param("abort", {"start": "interrupt"}, "held", set(), 130, id="start-interrupt"),
        pytest.param("abort", {"readiness": "error"}, "held", set(), 1, id="readiness-error"),
        pytest.param(
            "abort", {"readiness": "interrupt"}, "held", set(), 130, id="readiness-interrupt"
        ),
        pytest.param(
            "abort",
            {"readiness": "error", "marker write": "error"},
            "held",
            set(),
            1,
            id="marker-write-error-before-removal",
        ),
        pytest.param(
            "abort",
            {"unit": "failure", "marker write": "error"},
            "missing",
            set(),
            1,
            id="marker-write-error",
        ),
        pytest.param(
            "abort",
            {"unit": "failure", "marker write": "interrupt"},
            "missing",
            set(),
            130,
            id="marker-write-interrupt",
        ),
        pytest.param(
            "abort",
            {"unit": "failure", "marker sync": "error"},
            "held",
            set(),
            1,
            id="marker-sync-error",
        ),
        pytest.param(
            "abort",
            {"unit": "failure", "marker sync": "interrupt"},
            "held",
            set(),
            130,
            id="marker-sync-interrupt",
        ),
        pytest.param(
            "abort",
            {"unit": "failure", "shutdown": "error"},
            "held",
            APPLICATION,
            1,
            id="shutdown-error",
        ),
        pytest.param(
            "abort",
            {"readiness": "interrupt", "shutdown": "interrupt"},
            "held",
            {"dispatcher", "notifications", "worker"},
            130,
            id="shutdown-interrupt-after-readiness-interrupt",
        ),
        pytest.param(
            "abort", {"unit": "failure", "status": "error"}, "held", None, 1, id="status-error"
        ),
        pytest.param(
            "abort",
            {"unit": "failure", "status": "interrupt"},
            "held",
            None,
            130,
            id="status-interrupt",
        ),
        pytest.param(
            "abort", {"unit": "failure", "status": "timeout"}, "held", None, 1, id="status-timeout"
        ),
        pytest.param(
            "abort",
            {"unit": "failure", "marker write": "error", "status": "failure"},
            "missing",
            None,
            1,
            id="status-failure-without-marker",
        ),
        pytest.param(
            "abort",
            {"unit": "failure", "marker read": "error"},
            "unknown",
            set(),
            1,
            id="marker-read-error",
        ),
        pytest.param(
            "restore",
            {"unit": "failure", "marker write": "error", "shutdown": "stuck"},
            "missing",
            {"worker"},
            1,
            id="restore-stuck-worker-without-marker",
        ),
        pytest.param(
            "abort without session",
            {"unit": "failure", "marker write": "interrupt"},
            "missing",
            set(),
            130,
            id="empty-marker-lost",
        ),
    ],
)
def test_a_failed_start_attempts_the_shutdown_and_reports_the_state_it_observed(
    host: Host,
    commands: FakeCommands,
    uploads: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operation: str,
    faults: dict[str, str],
    marker: str,
    running: set[str] | None,
    code: int,
) -> None:
    monkeypatch.setattr(reset_database, "load_host", lambda _path: host)
    arguments = ["abort"]
    if operation == "abort without session":
        host.marker.parent.mkdir(parents=True)
        host.marker.touch()
        commands.running = set(SUPPORT)
    elif operation == "restore":
        name = prepared(host, uploads)
        reset_database.reset(host, name)
        arguments = ["restore", "--confirm", name]
    else:
        prepared(host, uploads)
    identity = host.marker.read_text()
    capsys.readouterr()
    attempted = inject_faults(monkeypatch, host, commands, faults)
    assert reset_database.main([str(host.config), *arguments]) == code
    assert any("stop" in call for call in attempted)
    assert commands.running & APPLICATION == (running or set())
    assert commands.running == SUPPORT | (running or set())
    assert host.marker.is_file() == (marker != "missing")
    if host.marker.is_file():
        assert host.marker.read_bytes().decode() == identity
    error = capsys.readouterr().err
    kinds = list(faults.values())
    first = "was interrupted." if kinds[0] == "interrupt" else "failed: "
    assert error.startswith(f"{arguments[0]} {first}")
    assert error.count("was interrupted.") == kinds.count("interrupt")
    assert error.count("Input/output error") == kinds.count("error")
    for point, fault in faults.items():
        assert (f"{point} failure detail" in error) == (fault in ("failure", "timeout"))
    shown = shlex.quote(identity)
    assert {
        "held": f"The maintenance marker {host.marker} is present and holds {shown}.",
        "missing": f"The maintenance marker {host.marker} is missing.",
        "unknown": f"The maintenance marker {host.marker} could not be read.",
    }[marker] in error
    if running is None:
        assert "Whether application containers run is unknown." in error
    elif running:
        assert f"Application containers still run: {', '.join(sorted(running))}." in error
    else:
        assert "No application container runs: the site is down." in error
    rerun = host.command(" ".join(arguments))
    write_back = f"write the identity back with printf %s {shown} > {host.marker} && sync"
    assert (write_back in error) == (marker != "held")
    assert (f"stop -t 660 {' '.join(sorted(running or ()))};" in error) == bool(running)
    assert (" ps --services --status running" in error) == (running is None)
    assert (f"Next: run {rerun} again." in error) == (marker == "held" and running == set())
    assert error.rstrip().endswith(f"run {rerun} again.")


CLEANUP_HEADER = "Writing the maintenance marker back and stopping the application again"
ORDERED_STOPS = [["gateway", "api", "web"], ["dispatcher"], ["worker", "notifications"]]
DEFERRED = (signal.SIGINT, signal.SIGTERM)


def raise_interrupt(_number: int, _frame: FrameType | None) -> None:
    raise KeyboardInterrupt


@pytest.fixture
def interrupting_handlers() -> Iterator[None]:
    """Makes SIGINT and SIGTERM raise KeyboardInterrupt, as Python's default SIGINT handler does."""
    previous = [(number, signal.signal(number, raise_interrupt)) for number in DEFERRED]
    yield
    for number, handler in previous:
        signal.signal(number, handler)


class FailingConsole(io.StringIO):
    """Breaks, closes or delivers a real signal while a line containing `trigger` is written."""

    def __init__(self, trigger: str, fault: str) -> None:
        super().__init__()
        self.trigger = trigger
        self.fault = fault
        self.broken = False

    def write(self, text: str) -> int:
        if self.trigger in text:
            match self.fault:
                case "broken pipe":
                    self.broken = True
                case "closed":
                    self.close()
                case name:
                    signal.raise_signal(signal.Signals[name])
        if self.broken:
            raise BrokenPipeError(32, "Broken pipe")
        return super().write(text)

    def flush(self) -> None:
        if self.broken:
            raise BrokenPipeError(32, "Broken pipe")
        super().flush()


def abort_after_failed_readiness(
    host: Host,
    commands: FakeCommands,
    monkeypatch: pytest.MonkeyPatch,
    console: contextlib.AbstractContextManager[Any],
) -> int:
    monkeypatch.setattr(reset_database, "load_host", lambda _path: host)
    commands.ready = False
    with console:
        return reset_database.main([str(host.config), "abort"])


def cleanup_stops(commands: FakeCommands) -> list[list[str]]:
    later = commands.calls[full_start(commands) :]
    return [call[call.index("stop") + 3 :] for call in later if "stop" in call]


@pytest.mark.usefixtures("interrupting_handlers")
@pytest.mark.parametrize(
    ("trigger", "fault"),
    [
        pytest.param(CLEANUP_HEADER, "broken pipe", id="broken-stdout-at-the-cleanup-header"),
        pytest.param("Stopping gateway", "closed", id="closed-stdout-before-the-first-stop"),
        pytest.param(CLEANUP_HEADER, "SIGINT", id="sigint-at-the-cleanup-header"),
        pytest.param("Stopping gateway", "SIGINT", id="sigint-after-the-marker-restoration"),
        pytest.param("Stopping dispatcher", "SIGINT", id="sigint-between-two-stops"),
        pytest.param("Stopping worker", "SIGTERM", id="sigterm-before-the-last-stop"),
    ],
)
def test_a_failed_start_shuts_down_despite_a_failing_console_or_a_signal(
    host: Host,
    commands: FakeCommands,
    uploads: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    trigger: str,
    fault: str,
) -> None:
    prepared(host, uploads)
    identity = host.marker.read_text()
    capsys.readouterr()
    console = contextlib.redirect_stdout(FailingConsole(trigger, fault))
    code = abort_after_failed_readiness(host, commands, monkeypatch, console)
    signalled = fault.startswith("SIG")
    assert code == (130 if signalled else 1)
    assert cleanup_stops(commands) == ORDERED_STOPS
    assert commands.running == SUPPORT
    assert [signal.getsignal(number) for number in DEFERRED] == [raise_interrupt] * 2
    assert host.marker.read_text() == identity
    error = capsys.readouterr().err
    assert error.startswith(
        "abort failed: The release started, but /health/ready/ did not answer ok.\n"
    )
    assert "Stopping the application" not in error
    deferred = f"{fault} arrived during the cleanup; every step was still attempted."
    assert (deferred in error) == signalled
    assert f"{host.marker} is present and holds {shlex.quote(identity)}." in error
    assert "No application container runs: the site is down." in error
    assert error.rstrip().endswith(f"Next: run {host.command('abort')} again.")


@pytest.mark.usefixtures("interrupting_handlers")
def test_a_failed_start_keeps_its_exit_status_when_the_report_cannot_be_written(
    host: Host, commands: FakeCommands, uploads: dict[str, bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared(host, uploads)
    identity = host.marker.read_text()
    console = contextlib.redirect_stderr(FailingConsole("abort failed", "broken pipe"))
    code = abort_after_failed_readiness(host, commands, monkeypatch, console)
    assert code == 1
    assert cleanup_stops(commands) == ORDERED_STOPS
    assert commands.running == SUPPORT
    assert [signal.getsignal(number) for number in DEFERRED] == [raise_interrupt] * 2
    assert host.marker.read_text() == identity


def test_command_line_names_the_next_step_after_an_interruption(
    host: Host, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupted(_host: Host) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(reset_database, "load_host", lambda _path: host)
    monkeypatch.setattr(reset_database, "prepare", interrupted)
    assert reset_database.main([str(host.config), "prepare"]) == 130
    error = capsys.readouterr().err
    assert "prepare was interrupted." in error
    assert f"run {host.command('abort')} to serve the previous release again" in error


def test_script_entry_point_prints_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["reset_database.py", "--help"])
    monkeypatch.setattr(sys, "path", list(sys.path))
    with pytest.raises(SystemExit) as finished:
        runpy.run_path(str(ROOT / "deploy/reset_database.py"), run_name="__main__")
    assert finished.value.code == 0
