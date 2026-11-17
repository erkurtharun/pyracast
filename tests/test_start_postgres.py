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
        start_postgres, "start_server", lambda command, env: commands.append(tuple(command))
    )
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
        assert windows_server_options(patch, tmp_path / "durable", ["--ephemeral"]) == (
            "-h 127.0.0.1 -p 55432"
        )
    with monkeypatch.context() as patch:
        assert windows_server_options(
            patch, tmp_path / "fast", ["--ephemeral", "--non-durable"]
        ) == ("-h 127.0.0.1 -p 55432 " + " ".join(NON_DURABLE))
    with monkeypatch.context() as patch:
        assert linux_server_arguments(patch, ["--ephemeral"]) == []
    with monkeypatch.context() as patch:
        assert linux_server_arguments(patch, ["--ephemeral", "--non-durable"]) == [
            "postgres",
            *NON_DURABLE,
        ]


def test_durable_data_lives_in_the_per_user_data_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert start_postgres.durable_root() == tmp_path / "local" / "pyracast"
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    assert start_postgres.durable_root() == tmp_path / "share" / "pyracast"
    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    assert start_postgres.durable_root() == tmp_path / "home" / ".local" / "share" / "pyracast"


def test_durable_data_is_never_placed_under_the_temporary_directory(tmp_path: Path) -> None:
    temporary = tmp_path / "temp"

    with pytest.raises(RuntimeError, match="temporary directory"):
        start_postgres.require_durable(temporary / "pyracast", temporary)
    start_postgres.require_durable(tmp_path / "data" / "pyracast", temporary)


def test_non_durable_settings_are_refused_for_a_cluster_that_keeps_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("no cluster may be provisioned")

    monkeypatch.setattr(start_postgres, "run", refuse)
    monkeypatch.setattr(start_postgres, "windows_workspace", refuse)
    monkeypatch.setattr(shutil, "which", refuse)

    with pytest.raises(SystemExit):
        start_postgres.main(["--non-durable"])


def windows_cluster(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, list[tuple[str, ...]], dict[str, bool]]:
    commands: list[tuple[str, ...]] = []
    server = {"running": False}
    root = tmp_path / "pyracast" / "postgres"

    def run(*args: str, env: dict[str, str] | None = None) -> None:
        commands.append(args)
        if Path(args[0]).name == "initdb.exe":
            data = Path(args[args.index("-D") + 1])
            data.mkdir(parents=True)
            (data / "PG_VERSION").write_text("18\n", encoding="utf-8")

    archive = tmp_path / "postgres.zip"
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr("pgsql/bin/pg_ctl.exe", "")
    monkeypatch.setattr(start_postgres, "run", run)
    monkeypatch.setattr(start_postgres, "start_server", lambda *args: run(*args[0]))
    monkeypatch.setattr(start_postgres, "accepts_connections", lambda port: server["running"])
    monkeypatch.setattr(start_postgres, "windows_preflight", lambda binary, env: None)
    monkeypatch.setattr(
        start_postgres, "ARCHIVE_SHA256", hashlib.sha256(archive.read_bytes()).hexdigest()
    )
    monkeypatch.setenv("PYRACAST_POSTGRES_ARCHIVE", str(archive))
    return root, commands, server


def test_windows_provisioning_creates_starts_and_then_reuses_one_cluster(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, commands, server = windows_cluster(monkeypatch, tmp_path)

    assert start_postgres.provision_windows(root, "55432", ()) == "created"
    assert [Path(command[0]).name for command in commands] == [
        "initdb.exe",
        "pg_ctl.exe",
        "createdb.exe",
    ]
    Path(tmp_path / "postgres.zip").unlink()
    commands.clear()

    assert start_postgres.provision_windows(root, "55432", ()) == "started"
    assert [Path(command[0]).name for command in commands] == ["pg_ctl.exe"]
    commands.clear()
    server["running"] = True

    assert start_postgres.provision_windows(root, "55432", ()) == "running"
    assert commands == []
    assert (root / "data" / "PG_VERSION").is_file()


def test_windows_provisioning_refuses_a_directory_that_is_not_a_cluster(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, commands, _ = windows_cluster(monkeypatch, tmp_path)
    foreign = root / "data" / "notes.txt"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("kept", encoding="utf-8")

    with pytest.raises(RuntimeError, match="not a PostgreSQL cluster"):
        start_postgres.provision_windows(root, "55432", ())

    assert foreign.read_text(encoding="utf-8") == "kept"
    assert commands == []


@pytest.mark.parametrize(
    ("state", "expected", "command"),
    [
        (None, "created", "run"),
        ("false", "started", "start"),
        ("true", "running", None),
    ],
)
def test_docker_provisioning_keeps_the_cluster_in_a_named_volume(
    monkeypatch: pytest.MonkeyPatch, state: str | None, expected: str, command: str | None
) -> None:
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(start_postgres, "run", lambda *args, env=None: commands.append(args))
    monkeypatch.setattr(start_postgres, "container_state", lambda docker, name: state)
    monkeypatch.setattr(start_postgres, "wait_for_container", lambda docker: None)

    assert start_postgres.provision_docker("docker", "55432", (), volume="data") == expected

    assert [arguments[1] for arguments in commands] == ([command] if command else [])
    if command == "run":
        assert "data:/var/lib/postgresql" in commands[0]
