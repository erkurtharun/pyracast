import json
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pika  # type: ignore[import-untyped]
import psutil
import psycopg
import pytest

from scripts import dev_dependencies as dependencies


@pytest.fixture
def local_dependencies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("PGHOST", "127.0.0.1")
    monkeypatch.setenv("PGPORT", "55432")
    monkeypatch.setenv("PGPASSWORD", "private-database-password")
    monkeypatch.setenv(
        "PYRACAST_RABBITMQ_URL", "amqp://guest:private-broker-password@127.0.0.1:5672/%2F"
    )
    data = tmp_path / "existing-postgres/data"
    data.mkdir(parents=True)
    (data / "postmaster.pid").write_text("4321\n", encoding="utf-8")
    (data / "PG_VERSION").write_text("18\n", encoding="utf-8")
    binary = tmp_path / "existing-postgres/pgsql/bin"
    binary.mkdir(parents=True)
    (binary / "pg_ctl.exe").touch()
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = (str(data),)
    connect = MagicMock(return_value=connection)
    monkeypatch.setattr(psycopg, "connect", connect)

    rabbit_root = tmp_path / "existing-rabbit"
    erlang = rabbit_root / "erlang"
    erlang.mkdir(parents=True)
    rabbit_state = rabbit_root / "state"
    rabbit_state.mkdir()
    config = rabbit_root / "rabbitmq.conf"
    config.touch()
    server = rabbit_root / "rabbitmq_server-4.4.0/sbin/rabbitmq-server.bat"
    server.parent.mkdir(parents=True)
    server.touch()
    environment = {
        "ERLANG_HOME": str(erlang),
        "RABBITMQ_BASE": str(rabbit_state),
        "RABBITMQ_CONFIG_FILE": str(config),
        "RABBITMQ_NODENAME": "pyracastci@localhost",
        "UNRELATED_PASSWORD": "must-not-be-recorded",
    }
    process = MagicMock()
    process.exe.return_value = str(binary / "postgres.exe")
    process.environ.return_value = environment
    monkeypatch.setattr(psutil, "Process", MagicMock(return_value=process))
    monkeypatch.setattr(
        psutil,
        "net_connections",
        lambda **kwargs: [
            SimpleNamespace(status=psutil.CONN_LISTEN, laddr=SimpleNamespace(port=5672), pid=9876)
        ],
    )
    broker = MagicMock()
    broker_connect = MagicMock(return_value=broker)
    monkeypatch.setattr(pika, "BlockingConnection", broker_connect)
    run = MagicMock()
    popen = MagicMock()
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", popen)
    return {
        "data": data,
        "pg_ctl": binary / "pg_ctl.exe",
        "connection": connection,
        "connect": connect,
        "broker": broker,
        "broker_connect": broker_connect,
        "run": run,
        "popen": popen,
    }


def test_reuses_authenticated_dependencies_and_records_only_recovery_metadata(
    local_dependencies: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    state: dict[str, Any] = {}

    dependencies.ensure_dependencies(state)

    assert state["postgres"] == {
        "data_dir": str(local_dependencies["data"]),
        "pg_ctl": str(local_dependencies["pg_ctl"]),
        "host": "127.0.0.1",
        "port": 55432,
    }
    assert set(state["rabbitmq"]["env"]) == set(dependencies.RABBIT_ENV)
    assert "password" not in json.dumps(state).lower()
    local_dependencies["connection"].execute.assert_any_call("SELECT 1")
    local_dependencies["broker"].close.assert_called_once()
    local_dependencies["run"].assert_not_called()
    local_dependencies["popen"].assert_not_called()
    assert capsys.readouterr() == ("", "")


def test_unavailable_postgres_without_recipe_never_initializes_a_database(
    local_dependencies: dict[str, Any], tmp_path: Path
) -> None:
    local_dependencies["connect"].side_effect = psycopg.OperationalError("private-password")

    with pytest.raises(RuntimeError, match="start the existing configured database") as failure:
        dependencies.ensure_dependencies({})

    assert "private-password" not in str(failure.value)
    local_dependencies["run"].assert_not_called()
    local_dependencies["popen"].assert_not_called()
    assert not (tmp_path / ".dev").exists()


def test_restarts_only_the_recorded_existing_postgres_data_directory(
    local_dependencies: dict[str, Any],
) -> None:
    state: dict[str, Any] = {}
    dependencies.ensure_dependencies(state)
    local_dependencies["connect"].side_effect = [
        psycopg.OperationalError(),
        local_dependencies["connection"],
    ]

    dependencies.ensure_dependencies(state)

    command = local_dependencies["run"].call_args.args[0]
    assert command[0] == str(local_dependencies["pg_ctl"])
    assert command[command.index("-D") + 1] == str(local_dependencies["data"])
    assert command[-1] == "start"
    assert "initdb" not in " ".join(command)
    assert "createdb" not in " ".join(command)


@pytest.mark.parametrize("change", ["missing-data", "different-port", "linux"])
def test_postgres_recovery_refuses_missing_or_unrelated_instances(
    local_dependencies: dict[str, Any], monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    state: dict[str, Any] = {}
    dependencies.ensure_dependencies(state)
    if change == "missing-data":
        (local_dependencies["data"] / "PG_VERSION").unlink()
    elif change == "different-port":
        monkeypatch.setenv("PGPORT", "55433")
    else:
        monkeypatch.setattr(sys, "platform", "linux")
    local_dependencies["connect"].side_effect = psycopg.OperationalError()

    with pytest.raises(RuntimeError):
        dependencies.ensure_dependencies(state)

    local_dependencies["run"].assert_not_called()


def test_healthy_database_without_metadata_permission_remains_usable(
    local_dependencies: dict[str, Any],
) -> None:
    local_dependencies["connection"].execute.side_effect = [
        MagicMock(),
        psycopg.errors.InsufficientPrivilege(),
    ]
    state: dict[str, Any] = {}

    dependencies.ensure_dependencies(state)

    assert state["postgres"] == {}
    local_dependencies["run"].assert_not_called()


def test_unavailable_broker_without_recipe_never_bootstraps_new_state(
    local_dependencies: dict[str, Any],
) -> None:
    local_dependencies["broker_connect"].side_effect = pika.exceptions.AMQPConnectionError(
        "private-broker-password"
    )

    with pytest.raises(RuntimeError, match="start the existing configured broker") as failure:
        dependencies.ensure_dependencies({})

    assert "private-broker-password" not in str(failure.value)
    local_dependencies["popen"].assert_not_called()


def test_restarts_broker_with_the_existing_state_and_whitelisted_environment(
    local_dependencies: dict[str, Any],
) -> None:
    state: dict[str, Any] = {}
    dependencies.ensure_dependencies(state)
    local_dependencies["broker_connect"].side_effect = [
        pika.exceptions.AMQPConnectionError(),
        local_dependencies["broker"],
    ]
    state["rabbitmq"]["env"]["UNTRUSTED_EXTRA"] = "not-inherited"

    dependencies.ensure_dependencies(state)

    call = local_dependencies["popen"].call_args
    assert call.args[0][-1] == state["rabbitmq"]["server"]
    assert call.kwargs["env"]["RABBITMQ_BASE"] == state["rabbitmq"]["env"]["RABBITMQ_BASE"]
    assert "UNTRUSTED_EXTRA" not in call.kwargs["env"]


def test_postgres_restart_errors_do_not_expose_command_output(
    local_dependencies: dict[str, Any],
) -> None:
    state: dict[str, Any] = {}
    dependencies.ensure_dependencies(state)
    local_dependencies["connect"].side_effect = psycopg.OperationalError()
    local_dependencies["run"].side_effect = subprocess.CalledProcessError(
        1, ["private-argument"], stderr="private-output"
    )

    with pytest.raises(RuntimeError, match="could not be restarted") as failure:
        dependencies.ensure_dependencies(state)

    assert "private" not in str(failure.value)


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows inherited pipe regression")
def test_postgres_restart_returns_while_the_started_server_keeps_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    data = tmp_path / "existing-data"
    data.mkdir()
    (data / "PG_VERSION").write_text("18\n", encoding="utf-8")
    pg_ctl = tmp_path / "pg_ctl.exe"
    pg_ctl.touch()
    marker = tmp_path / "server-identity.json"
    code = (
        "import json, os, subprocess, sys\n"
        "from pathlib import Path\n"
        "import psutil\n"
        "child = subprocess.Popen([sys._base_executable, '-c', "
        "'import threading; threading.Event().wait(10)'], close_fds=False)\n"
        f"Path({str(marker)!r}).write_text(json.dumps({{'pid': child.pid, "
        "'created': psutil.Process(child.pid).create_time()}))\n"
        "os._exit(0)\n"
    )
    original_run = subprocess.run

    def fake_pg_ctl(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        assert args[0] == str(pg_ctl)
        return original_run([sys.executable, "-c", code], **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_pg_ctl)
    try:
        dependencies._restart_postgres(
            {"data_dir": str(data), "pg_ctl": str(pg_ctl), "host": "127.0.0.1", "port": 55432},
            "127.0.0.1",
            55432,
        )

        record = json.loads(marker.read_text())
        daemon = psutil.Process(record["pid"])
        assert daemon.create_time() == record["created"]
        assert daemon.is_running()
    finally:
        if marker.is_file():
            record = json.loads(marker.read_text())
            with suppress(psutil.NoSuchProcess):
                daemon = psutil.Process(record["pid"])
                if daemon.create_time() == record["created"]:
                    daemon.terminate()
                    daemon.wait(timeout=5)
