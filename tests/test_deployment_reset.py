import base64
import hashlib
import io
import json
import re
import runpy
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deploy import reset_database
from deploy.reset_database import Host, Refused

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

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.inputs: list[str] = []
        self.running = APPLICATION | SUPPORT
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
        self.calls.append(list(command))
        if isinstance(kwargs.get("input"), str):
            self.inputs.append(kwargs["input"])
        if self.fail is not None and self.fail in " ".join(command):
            raise subprocess.CalledProcessError(1, command, "", "fixture failure detail")
        stdout = ""
        if command[0] == "curl":
            return subprocess.CompletedProcess(
                command, 0 if self.ready else 22, "ok\n" if self.ready else "", ""
            )
        if "ps" in command:
            stdout = "".join(f"{service}\n" for service in sorted(self.running))
        elif "stop" in command:
            self.running -= set(command[command.index("stop") + 3 :])
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
        runtime_secret_version="projects/example/secrets/runtime/versions/7",
        marker=tmp_path / "lib/release-blocked",
        lock=tmp_path / "lock/release.lock",
        state=tmp_path / "lib/reset",
        environment=environment,
    )


@pytest.fixture
def commands(monkeypatch: pytest.MonkeyPatch) -> FakeCommands:
    fake = FakeCommands()
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
    with pytest.raises(Refused, match="release-blocked"):
        reset_database.prepare(host)
    assert not any("stop" in call for call in commands.calls)
    assert uploads == {}


def test_prepare_refuses_when_the_site_is_not_ready(
    host: Host, commands: FakeCommands, uploads: dict[str, bytes]
) -> None:
    commands.ready = False
    with pytest.raises(Refused, match="health/ready"):
        reset_database.prepare(host)
    assert not host.marker.exists()
    assert commands.running == APPLICATION | SUPPORT


def test_prepare_refuses_while_another_operation_holds_the_release_lock(
    host: Host, commands: FakeCommands, uploads: dict[str, bytes]
) -> None:
    commands.lock_held = True
    with pytest.raises(Refused, match="release lock"):
        reset_database.prepare(host)
    assert commands.calls == []
    assert not host.marker.exists()


def test_prepare_records_nothing_when_the_verification_restore_differs(
    host: Host, commands: FakeCommands, uploads: dict[str, bytes]
) -> None:
    commands.verification_counts = {"accounts_tenant": 1, "django_migrations": 88}
    with pytest.raises(RuntimeError, match="accounts_tenant: expected 2, found 1"):
        reset_database.prepare(host)
    assert host.marker.exists()
    name = next(iter(uploads))
    with pytest.raises(Refused, match="No verified archive"):
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
    start = commands.calls[commands.index("up --detach --wait --wait-timeout 180")]
    assert start[-1] == "180"
    assert commands.calls[-1][:3] == ["sync", "-f", str(host.marker.parent)]
    assert "--retry" in commands.calls[-2]
    assert "serves again" in capsys.readouterr().out


def test_abort_keeps_the_marker_when_readiness_fails(
    host: Host, commands: FakeCommands, uploads: dict[str, bytes]
) -> None:
    prepared(host, uploads)
    commands.ready = False
    with pytest.raises(RuntimeError, match="did not answer ok"):
        reset_database.abort(host)
    assert host.marker.exists()


def test_abort_refuses_once_the_database_was_replaced(
    host: Host, commands: FakeCommands, uploads: dict[str, bytes]
) -> None:
    name = prepared(host, uploads)
    reset_database.reset(host, name)
    calls = len(commands.calls)
    with pytest.raises(Refused, match=f"restore --confirm {name}"):
        reset_database.abort(host)
    assert commands.calls[calls:] == []
    assert host.marker.exists()


def test_abort_writes_runtime_secrets_and_rearms_the_unit_after_a_host_restart(
    host: Host, commands: FakeCommands, uploads: dict[str, bytes]
) -> None:
    prepared(host, uploads)
    (host.environment / "runtime.env").unlink()
    reset_database.abort(host)
    secrets = commands.calls.index(
        [sys.executable, str(host.release / "deploy/runtime_secrets.py"), str(host.config)]
    )
    assert secrets < commands.index("up --detach --wait --wait-timeout 180")
    assert commands.calls[-1] == ["systemctl", "start", "pyracast.service"]
    assert not host.marker.exists()


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
    ],
)
def test_reset_refuses_before_any_destructive_step(
    host: Host, commands: FakeCommands, uploads: dict[str, bytes], condition: str
) -> None:
    name = prepared(host, uploads)
    match condition:
        case "unknown-object":
            name = FOREIGN_OBJECT
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
    with pytest.raises(Refused):
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
    assert commands.index("DROP DATABASE") < commands.calls.index(restore)
    assert not host.marker.exists()
    assert commands.running == APPLICATION | SUPPORT
    assert "serves again" in capsys.readouterr().out
    reset_database.abort(host)


def test_restore_keeps_the_site_stopped_when_restored_counts_differ(
    host: Host, commands: FakeCommands, uploads: dict[str, bytes]
) -> None:
    name = prepared(host, uploads)
    commands.restore_drops_a_table = True
    with pytest.raises(RuntimeError, match="django_migrations: expected 88, found None"):
        reset_database.restore(host, name)
    assert host.marker.exists()
    assert commands.running == SUPPORT


def fake_bucket(monkeypatch: pytest.MonkeyPatch, content: bytes, *, md5: str | None = None) -> None:
    checksum = md5 or base64.b64encode(hashlib.md5(content).digest()).decode()  # noqa: S324

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
    with pytest.raises(Refused, match="checksum"):
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


def test_script_entry_point_prints_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["reset_database.py", "--help"])
    monkeypatch.setattr(sys, "path", list(sys.path))
    with pytest.raises(SystemExit) as finished:
        runpy.run_path(str(ROOT / "deploy/reset_database.py"), run_name="__main__")
    assert finished.value.code == 0
