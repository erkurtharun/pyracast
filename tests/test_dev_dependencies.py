import json
import os
import sys
import time
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
from scripts import start_postgres, start_rabbitmq


@pytest.fixture
def local_dependencies(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(dependencies, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setenv("PGHOST", "127.0.0.1")
    monkeypatch.setenv("PGPORT", "55432")
    monkeypatch.setenv("PGDATABASE", "pyracast")
    monkeypatch.setenv("PGPASSWORD", "private-database-password")
    monkeypatch.setenv(
        "PYRACAST_RABBITMQ_URL", "amqp://guest:private-broker-password@127.0.0.1:5672/%2F"
    )
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connect = MagicMock(return_value=connection)
    monkeypatch.setattr(psycopg, "connect", connect)
    broker = MagicMock()
    broker_connect = MagicMock(return_value=broker)
    monkeypatch.setattr(pika, "BlockingConnection", broker_connect)
    postgres = MagicMock()
    rabbitmq = MagicMock()
    monkeypatch.setattr(start_postgres, "main", postgres)
    monkeypatch.setattr(start_rabbitmq, "main", rabbitmq)
    return {
        "connection": connection,
        "connect": connect,
        "broker": broker,
        "broker_connect": broker_connect,
        "postgres": postgres,
        "rabbitmq": rabbitmq,
    }


def test_reuses_available_dependencies_and_names_their_targets(
    local_dependencies: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    dependencies.ensure_dependencies()

    local_dependencies["connection"].execute.assert_called_once_with("SELECT 1")
    local_dependencies["broker"].close.assert_called_once()
    local_dependencies["postgres"].assert_not_called()
    local_dependencies["rabbitmq"].assert_not_called()
    output = capsys.readouterr().out
    assert "PostgreSQL 127.0.0.1:55432/pyracast" in output
    assert "RabbitMQ 127.0.0.1:5672" in output
    assert "private" not in output


def test_starts_the_durable_database_when_the_loopback_database_is_down(
    local_dependencies: dict[str, Any],
) -> None:
    local_dependencies["connect"].side_effect = [
        psycopg.OperationalError("private-password"),
        local_dependencies["connection"],
    ]

    dependencies.ensure_dependencies()

    local_dependencies["postgres"].assert_called_once_with(["--port", "55432"])


def test_provisions_the_database_on_the_port_it_probes(
    local_dependencies: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PGPORT")
    local_dependencies["connect"].side_effect = [
        psycopg.OperationalError("down"),
        local_dependencies["connection"],
    ]

    dependencies.ensure_dependencies()

    assert {call.kwargs["port"] for call in local_dependencies["connect"].call_args_list} == {
        "5432"
    }
    local_dependencies["postgres"].assert_called_once_with(["--port", "5432"])


def test_reports_a_database_that_stays_down_without_its_connection_details(
    local_dependencies: dict[str, Any],
) -> None:
    local_dependencies["connect"].side_effect = psycopg.OperationalError("private-password")

    with pytest.raises(RuntimeError, match="did not become available") as failure:
        dependencies.ensure_dependencies()

    assert "private" not in str(failure.value)


@pytest.mark.parametrize("host", ["db.example.com", "10.0.0.5", "::1"])
def test_refuses_a_database_outside_the_ipv4_loopback_the_helper_serves(
    local_dependencies: dict[str, Any], monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    monkeypatch.setenv("PGHOST", host)

    with pytest.raises(RuntimeError, match="loopback"):
        dependencies.ensure_dependencies()

    local_dependencies["connect"].assert_not_called()
    local_dependencies["postgres"].assert_not_called()


@pytest.mark.parametrize(
    "error",
    [
        psycopg.errors.InvalidPassword("private-password"),
        psycopg.errors.InvalidAuthorizationSpecification("private-role"),
    ],
)
def test_rejected_database_credentials_are_reported_as_authentication(
    local_dependencies: dict[str, Any], error: psycopg.Error
) -> None:
    local_dependencies["connect"].side_effect = error

    with pytest.raises(RuntimeError, match="rejected the configured credentials") as failure:
        dependencies.ensure_dependencies()

    assert "private" not in str(failure.value)
    local_dependencies["postgres"].assert_not_called()


def test_starts_the_durable_broker_when_it_is_down_on_windows(
    local_dependencies: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYRACAST_RABBITMQ_URL", start_rabbitmq.QUEUE_URL)
    local_dependencies["broker_connect"].side_effect = [
        pika.exceptions.AMQPConnectionError("private-broker-password"),
        local_dependencies["broker"],
    ]

    dependencies.ensure_dependencies()

    local_dependencies["rabbitmq"].assert_called_once_with([])


@pytest.mark.parametrize(
    "url",
    ["amqp://guest:guest@127.0.0.1:5673/%2F", "amqp://guest:guest@[::1]:5672/%2F"],
)
def test_a_broker_target_the_helper_does_not_serve_is_refused_before_provisioning(
    local_dependencies: dict[str, Any], monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv("PYRACAST_RABBITMQ_URL", url)
    local_dependencies["broker_connect"].side_effect = pika.exceptions.AMQPConnectionError()

    with pytest.raises(RuntimeError, match=r"127\.0\.0\.1:5672"):
        dependencies.ensure_dependencies()

    local_dependencies["rabbitmq"].assert_not_called()


def test_rejected_broker_credentials_are_reported_as_authentication(
    local_dependencies: dict[str, Any],
) -> None:
    local_dependencies["broker_connect"].side_effect = pika.exceptions.ProbableAuthenticationError(
        "private-broker-password"
    )

    with pytest.raises(RuntimeError, match="rejected the configured credentials") as failure:
        dependencies.ensure_dependencies()

    assert "private" not in str(failure.value)
    local_dependencies["rabbitmq"].assert_not_called()


def test_a_broker_that_is_down_off_windows_points_to_the_documented_container(
    local_dependencies: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dependencies, "sys", SimpleNamespace(platform="linux"))
    local_dependencies["broker_connect"].side_effect = pika.exceptions.AMQPConnectionError()

    with pytest.raises(RuntimeError, match="README"):
        dependencies.ensure_dependencies()

    local_dependencies["rabbitmq"].assert_not_called()


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows inherited pipe regression")
def test_server_start_returns_while_the_started_server_keeps_running(tmp_path: Path) -> None:
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
    try:
        start_postgres.start_server([sys.executable, "-c", code], dict(os.environ))

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


def test_waits_for_the_broker_to_complete_amqp_startup(
    local_dependencies: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYRACAST_RABBITMQ_URL", start_rabbitmq.QUEUE_URL)
    local_dependencies["broker_connect"].side_effect = [
        pika.exceptions.AMQPConnectionError(),
        pika.exceptions.AMQPConnectionError(),
        local_dependencies["broker"],
    ]
    monkeypatch.setattr(time, "sleep", lambda seconds: None)

    dependencies.ensure_dependencies()

    local_dependencies["rabbitmq"].assert_called_once_with([])
    local_dependencies["broker"].close.assert_called_once()


@pytest.mark.parametrize(
    "url",
    [
        "amqp://custom:secret@127.0.0.1:5672/%2F",
        "amqp://guest:secret@127.0.0.1:5672/%2F",
        "amqp://guest:guest@127.0.0.1:5672/custom",
        "amqps://guest:guest@127.0.0.1:5672/%2F",
    ],
)
def test_automatic_broker_provisioning_requires_the_supported_configuration(
    local_dependencies: dict[str, Any], monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv("PYRACAST_RABBITMQ_URL", url)
    local_dependencies["broker_connect"].side_effect = pika.exceptions.AMQPConnectionError()

    with pytest.raises(RuntimeError, match="guest") as failure:
        dependencies.ensure_dependencies()

    assert "secret" not in str(failure.value)
    local_dependencies["rabbitmq"].assert_not_called()
