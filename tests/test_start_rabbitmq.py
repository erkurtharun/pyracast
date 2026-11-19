import sys
import tempfile
from importlib import import_module
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts import start_rabbitmq


def windows_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[str], list[Path], dict[str, bool]]:
    downloads: list[str] = []
    starts: list[Path] = []
    broker = {"running": False}

    def download(url: str, digest: str, archive: Path, destination: Path) -> None:
        downloads.append(url)
        if url == start_rabbitmq.ERLANG_URL:
            (destination / "bin").mkdir(parents=True, exist_ok=True)
            (destination / "bin" / "erl.exe").touch()
        else:
            server = destination / "rabbitmq_server-4.4.1" / "sbin" / "rabbitmq-server.bat"
            server.parent.mkdir(parents=True)
            server.touch()

    monkeypatch.setattr(start_rabbitmq, "download_and_extract", download)
    monkeypatch.setattr(start_rabbitmq, "port_open", lambda: broker["running"])
    monkeypatch.setattr(start_rabbitmq, "amqp_ready", lambda: broker["running"])
    monkeypatch.setattr(start_rabbitmq, "start_broker", lambda root, server: starts.append(root))
    return downloads, starts, broker


def test_windows_provisioning_installs_once_and_reuses_the_broker_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    downloads, starts, broker = windows_broker(monkeypatch)
    root = tmp_path / "pyracast" / "rabbitmq"

    assert start_rabbitmq.provision_windows(root) == "started"
    assert downloads == [start_rabbitmq.ERLANG_URL, start_rabbitmq.RABBITMQ_URL]
    assert (root / "rabbitmq.conf").read_text(encoding="utf-8") == (
        "listeners.tcp.1 = 127.0.0.1:5672\n"
    )
    downloads.clear()

    assert start_rabbitmq.provision_windows(root) == "started"
    assert downloads == []
    broker["running"] = True

    assert start_rabbitmq.provision_windows(root) == "running"
    assert starts == [root, root]


def test_durable_broker_state_is_never_placed_under_the_temporary_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    downloads, _, _ = windows_broker(monkeypatch)

    with pytest.raises(RuntimeError, match="temporary directory"):
        start_rabbitmq.main([])

    assert downloads == []


def test_incomplete_erlang_installation_is_repaired(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    downloads, _, _ = windows_broker(monkeypatch)
    (tmp_path / "erlang" / "bin").mkdir(parents=True)
    monkeypatch.setattr(start_rabbitmq, "start_broker", lambda *args: None)

    assert start_rabbitmq.provision_windows(tmp_path) == "started"
    assert downloads == [start_rabbitmq.ERLANG_URL, start_rabbitmq.RABBITMQ_URL]


def test_occupied_port_requires_an_amqp_connection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pika = import_module("pika")

    monkeypatch.setattr(start_rabbitmq, "port_open", lambda: True)
    monkeypatch.setattr(
        pika, "BlockingConnection", MagicMock(side_effect=pika.exceptions.AMQPConnectionError())
    )

    with pytest.raises(RuntimeError, match="AMQP"):
        start_rabbitmq.provision_windows(tmp_path / "unused")

    assert not (tmp_path / "unused").exists()
