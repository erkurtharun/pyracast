import hashlib
import json
import shutil
import socket
import subprocess
import sys
import zipfile
from contextlib import nullcontext
from pathlib import Path
from uuid import UUID

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
    monkeypatch.setattr(start_postgres, "verify_windows_server", lambda *args: None)
    monkeypatch.setattr(
        start_postgres, "start_server", lambda command, env: commands.append(tuple(command))
    )
    monkeypatch.setattr(
        start_postgres,
        "execute",
        lambda command, **options: subprocess.CompletedProcess(command, 0, stdout=""),
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


def test_the_port_argument_overrides_pgport(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    options = windows_server_options(
        monkeypatch, tmp_path / "port", ["--ephemeral", "--port", "5432"]
    )

    assert options == "-h 127.0.0.1 -p 5432"


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
    server = {"running": False, "database": False, "createdb_fails": False}
    root = tmp_path / "pyracast" / "postgres"

    def run(*args: str, env: dict[str, str] | None = None) -> None:
        commands.append(args)
        if Path(args[0]).name == "initdb.exe":
            data = Path(args[args.index("-D") + 1])
            data.mkdir(parents=True)
            (data / "PG_VERSION").write_text("18\n", encoding="utf-8")
        if Path(args[0]).name == "createdb.exe":
            if server["createdb_fails"]:
                raise subprocess.CalledProcessError(1, args)
            server["database"] = True

    def execute(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        if Path(command[0]).name == "pg_isready.exe":
            return subprocess.CompletedProcess(command, 0, stdout="")
        assert Path(command[0]).name == "psql.exe"
        assert {"-w", "postgres"} <= set(command)
        if command[-1] == "SHOW data_directory":
            return subprocess.CompletedProcess(command, 0, stdout=str(root / "data"))
        return subprocess.CompletedProcess(command, 0, stdout="1\n" if server["database"] else "")

    archive = tmp_path / "postgres.zip"
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr("pgsql/bin/pg_ctl.exe", "")
    monkeypatch.setattr(start_postgres, "run", run)
    monkeypatch.setattr(start_postgres, "execute", execute)
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


@pytest.mark.parametrize(
    ("running", "expected", "names"),
    [(True, "running", ["createdb.exe"]), (False, "started", ["pg_ctl.exe", "createdb.exe"])],
)
def test_windows_provisioning_creates_the_database_an_interrupted_initialization_left_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    running: bool,
    expected: str,
    names: list[str],
) -> None:
    root, commands, server = windows_cluster(monkeypatch, tmp_path)
    server["createdb_fails"] = True
    with pytest.raises(subprocess.CalledProcessError):
        start_postgres.provision_windows(root, "55432", ())
    server.update(createdb_fails=False, running=running)
    commands.clear()

    assert start_postgres.provision_windows(root, "55432", ()) == expected

    assert [Path(command[0]).name for command in commands] == names
    assert server["database"]


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


def inspected(running: bool, volumes: list[str], command: list[str]) -> str:
    mounts = [
        {"Type": "volume", "Name": name, "Destination": "/var/lib/postgresql", "RW": True}
        for name in volumes
    ]
    return json.dumps(
        [
            {
                "State": {"Running": running},
                "Mounts": mounts,
                "Config": {"Cmd": command, "Env": ["PGDATA=/var/lib/postgresql/18/docker"]},
                "HostConfig": {
                    "PortBindings": {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "55432"}]}
                },
            }
        ]
    )


def fake_docker(
    monkeypatch: pytest.MonkeyPatch, containers: dict[str, str]
) -> list[tuple[str, ...]]:
    commands: list[tuple[str, ...]] = []

    def execute(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        if command[-1] in containers:
            return subprocess.CompletedProcess(command, 0, stdout=containers[command[-1]])
        return subprocess.CompletedProcess(command, 1, stdout="[]")

    monkeypatch.setattr(start_postgres, "execute", execute)
    monkeypatch.setattr(start_postgres, "run", lambda *args, env=None: commands.append(args))
    monkeypatch.setattr(start_postgres, "wait_for_container", lambda *args: None)
    return commands


@pytest.mark.parametrize(
    ("existing", "expected", "command"),
    [
        (None, "created", "run"),
        (inspected(False, ["data"], ["postgres"]), "started", "start"),
        (inspected(True, ["data"], ["postgres"]), "running", None),
    ],
)
def test_docker_provisioning_keeps_the_cluster_in_a_named_volume(
    monkeypatch: pytest.MonkeyPatch, existing: str | None, expected: str, command: str | None
) -> None:
    containers = {} if existing is None else {"pyracast-postgres": existing}
    commands = fake_docker(monkeypatch, containers)

    assert start_postgres.provision_docker("docker", "55432", (), volume="data") == expected

    assert [arguments[1] for arguments in commands] == ([command] if command else [])
    if command == "run":
        assert "data:/var/lib/postgresql" in commands[0]
        assert "pyracast-postgres" in commands[0]


@pytest.mark.parametrize(
    "existing",
    [
        inspected(True, ["4f1c0e"], ["postgres"]),
        inspected(False, [], ["postgres"]),
        inspected(True, ["data"], ["postgres", *NON_DURABLE]),
    ],
)
def test_docker_provisioning_refuses_a_container_that_does_not_keep_durable_data(
    monkeypatch: pytest.MonkeyPatch, existing: str
) -> None:
    commands = fake_docker(monkeypatch, {"pyracast-postgres": existing})

    with pytest.raises(RuntimeError, match="nothing was changed"):
        start_postgres.provision_docker("docker", "55432", (), volume="data")

    assert commands == []


def test_a_throwaway_docker_cluster_never_takes_the_durable_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = fake_docker(
        monkeypatch, {"pyracast-postgres": inspected(True, ["data"], ["postgres"])}
    )
    monkeypatch.setattr(start_postgres, "uuid4", lambda: UUID(int=1))

    assert start_postgres.provision_docker("docker", "55432", NON_DURABLE, volume=None) == (
        "created"
    )

    (container,) = commands
    assert container[container.index("--name") + 1] == (
        "pyracast-postgres-ephemeral-00000000000000000000000000000001"
    )
    assert "--volume" not in container


@pytest.mark.parametrize(
    ("destination", "writable"), [("/other", True), ("/var/lib/postgresql", False)]
)
def test_durable_container_requires_a_writable_volume_at_the_data_location(
    monkeypatch: pytest.MonkeyPatch, destination: str, writable: bool
) -> None:
    details = json.loads(inspected(True, ["data"], ["postgres"]))
    details[0]["Mounts"][0].update(Destination=destination, RW=writable)
    commands = fake_docker(monkeypatch, {"pyracast-postgres": json.dumps(details)})

    with pytest.raises(RuntimeError, match="nothing was changed"):
        start_postgres.provision_docker("docker", "55432", (), volume="data")

    assert commands == []


@pytest.mark.parametrize("running", [False, True])
def test_existing_container_requires_the_requested_loopback_port(
    monkeypatch: pytest.MonkeyPatch, running: bool
) -> None:
    details = json.loads(inspected(running, ["data"], ["postgres"]))
    details[0]["HostConfig"] = {
        "PortBindings": {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "55433"}]}
    }
    commands = fake_docker(monkeypatch, {"pyracast-postgres": json.dumps(details)})

    with pytest.raises(RuntimeError, match="port"):
        start_postgres.provision_docker("docker", "55432", (), volume="data")

    assert commands == []


def test_running_container_waits_until_postgresql_accepts_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_docker(monkeypatch, {"pyracast-postgres": inspected(True, ["data"], ["postgres"])})

    def not_ready(*args: str) -> None:
        raise RuntimeError("PostgreSQL did not become ready")

    monkeypatch.setattr(start_postgres, "wait_for_container", not_ready)
    with pytest.raises(RuntimeError, match="did not become ready"):
        start_postgres.provision_docker("docker", "55432", (), volume="data")


@pytest.mark.parametrize("port", ["0", "-1", "65536", "invalid"])
@pytest.mark.parametrize("source", ["argument", "environment"])
def test_invalid_port_is_refused_before_provisioning(
    monkeypatch: pytest.MonkeyPatch, port: str, source: str
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("provisioning must not begin")

    monkeypatch.setattr(start_postgres, "durable_root", refuse)
    monkeypatch.setattr(shutil, "which", refuse)
    monkeypatch.setenv("PGPORT", port if source == "environment" else "55432")
    with pytest.raises(SystemExit):
        start_postgres.main(["--port", port] if source == "argument" else [])


def test_windows_server_identity_is_checked_before_creating_a_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, commands, server = windows_cluster(monkeypatch, tmp_path)
    data = root / "data"
    data.mkdir(parents=True)
    (data / "PG_VERSION").write_text("18\n", encoding="utf-8")
    server["running"] = True

    def execute(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout=str(tmp_path / "foreign"))

    monkeypatch.setattr(start_postgres, "execute", execute)
    with pytest.raises(RuntimeError, match="different PostgreSQL cluster"):
        start_postgres.provision_windows(root, "55432", ())

    assert commands == []


def test_each_throwaway_cluster_uses_a_fresh_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = fake_docker(
        monkeypatch, {"pyracast-postgres": inspected(True, ["data"], ["postgres"])}
    )

    for _ in range(2):
        assert start_postgres.provision_docker("docker", "55432", (), volume=None) == "created"

    names = [command[command.index("--name") + 1] for command in commands]
    assert len(set(names)) == 2
    assert all(name.startswith("pyracast-postgres-ephemeral-") for name in names)
    assert all("--volume" not in command for command in commands)


@pytest.mark.parametrize(
    "data_directory",
    ["/outside/pgdata", "/var/lib/postgresql2/data", "/var/lib/postgresql/../../../outside"],
)
def test_durable_container_requires_data_inside_the_named_volume(
    monkeypatch: pytest.MonkeyPatch, data_directory: str
) -> None:
    details = json.loads(inspected(True, ["data"], ["postgres"]))
    details[0]["Config"]["Env"] = [f"PGDATA={data_directory}"]
    commands = fake_docker(monkeypatch, {"pyracast-postgres": json.dumps(details)})

    with pytest.raises(RuntimeError, match="nothing was changed"):
        start_postgres.provision_docker("docker", "55432", (), volume="data")

    assert commands == []


def test_a_throwaway_docker_cluster_runs_the_production_postgresql_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = fake_docker(monkeypatch, {})

    assert start_postgres.provision_docker("docker", "55432", (), volume=None) == "created"
    assert start_postgres.provision_docker("docker", "55432", (), volume="data") == "created"

    throwaway, durable = commands
    assert start_postgres.THROWAWAY_POSTGRES_IMAGE in throwaway
    assert start_postgres.POSTGRES_IMAGE not in throwaway
    assert start_postgres.POSTGRES_IMAGE in durable


def test_a_throwaway_windows_cluster_installs_the_production_minor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fetched: list[str] = []
    content = tmp_path / "content.zip"
    with zipfile.ZipFile(content, "w") as target:
        target.writestr("pgsql/bin/README", "")

    def retrieve(url: str, path: Path) -> None:
        fetched.append(url)
        shutil.copyfile(content, path)

    monkeypatch.delenv("PYRACAST_POSTGRES_ARCHIVE", raising=False)
    monkeypatch.setattr(start_postgres.urllib.request, "urlretrieve", retrieve)
    digest = hashlib.sha256(content.read_bytes()).hexdigest()
    monkeypatch.setattr(start_postgres, "THROWAWAY_ARCHIVE_SHA256", digest)

    start_postgres.install_binaries(tmp_path / "throwaway", throwaway=True)

    assert fetched == [start_postgres.THROWAWAY_ARCHIVE_URL]
    assert start_postgres.THROWAWAY_ARCHIVE_URL != start_postgres.ARCHIVE_URL
    assert (tmp_path / "throwaway" / "pgsql" / "bin" / "README").is_file()
