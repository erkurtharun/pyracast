import hashlib
import shutil
import socket
import subprocess
import sys
import zipfile
from contextlib import nullcontext
from pathlib import Path

import pytest

from scripts import start_postgres

NON_DURABLE = ("-c", "fsync=off", "-c", "synchronous_commit=off", "-c", "full_page_writes=off")


def record_commands(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    commands: list[tuple[str, ...]] = []

    def run(*args: str, env: dict[str, str] | None = None) -> None:
        commands.append(args)

    monkeypatch.setattr(start_postgres, "run", run)
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: nullcontext())
    monkeypatch.setenv("PGPORT", "55432")
    return commands


def windows_server_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, arguments: list[str]
) -> str:
    commands = record_commands(monkeypatch)
    tmp_path.mkdir()
    archive = tmp_path / "postgres.zip"
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr("pgsql/bin/README", "")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "argv", ["start_postgres.py", *arguments])
    monkeypatch.setattr(start_postgres, "windows_workspace", lambda: tmp_path)
    monkeypatch.setattr(start_postgres, "windows_preflight", lambda binary, env: None)
    monkeypatch.setattr(
        start_postgres, "ARCHIVE_SHA256", hashlib.sha256(archive.read_bytes()).hexdigest()
    )
    monkeypatch.setenv("PYRACAST_POSTGRES_ARCHIVE", str(archive))

    start_postgres.main()

    (start,) = (command for command in commands if Path(command[0]).name == "pg_ctl.exe")
    return start[start.index("-o") + 1]


def linux_server_arguments(monkeypatch: pytest.MonkeyPatch, arguments: list[str]) -> list[str]:
    commands = record_commands(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "argv", ["start_postgres.py", *arguments])
    monkeypatch.setattr(shutil, "which", lambda name: "docker")
    monkeypatch.setattr(
        subprocess, "run", lambda args, **kwargs: subprocess.CompletedProcess(args, 0)
    )

    start_postgres.main()

    (container,) = commands
    return list(container[container.index(start_postgres.POSTGRES_IMAGE) + 1 :])


def test_non_durable_flag_is_opt_in_on_both_platforms(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with monkeypatch.context() as patch:
        assert windows_server_options(patch, tmp_path / "durable", []) == "-h 127.0.0.1 -p 55432"
    with monkeypatch.context() as patch:
        assert windows_server_options(patch, tmp_path / "fast", ["--non-durable"]) == (
            "-h 127.0.0.1 -p 55432 " + " ".join(NON_DURABLE)
        )
    with monkeypatch.context() as patch:
        assert linux_server_arguments(patch, []) == []
    with monkeypatch.context() as patch:
        assert linux_server_arguments(patch, ["--non-durable"]) == ["postgres", *NON_DURABLE]
