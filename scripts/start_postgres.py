import argparse
import csv
import hashlib
import json
import os
import posixpath
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

# The development cluster that running work shares keeps its minor until it is upgraded on
# purpose; throwaway clusters (CI) run production's minor and, on Linux, its base image.
ARCHIVE_VERSION = "18.6"
ARCHIVE_URL = "https://sbp.enterprisedb.com/getfile.jsp?fileid=1260609"
ARCHIVE_SHA256 = "e2246ba91d22345bc3d017586c09ede52d9df180b1eeb480f050445f1cad84e2"
POSTGRES_IMAGE = (
    "postgres:18.6@sha256:e3d24f6906d0760bf4da0dae872415391ededefad23fd471c47dc53b0ddf3c42"
)
THROWAWAY_ARCHIVE_VERSION = "18.7"
THROWAWAY_ARCHIVE_URL = "https://sbp.enterprisedb.com/getfile.jsp?fileid=1260725"
THROWAWAY_ARCHIVE_SHA256 = "74f3740a2e7844118857f297c2067d226f8f2b0652de96022ce54bb27cd60a79"
PRODUCTION_DOCKERFILE = Path(__file__).resolve().parents[1] / "deploy" / "postgres" / "Dockerfile"
CONTAINER = "pyracast-postgres"
EPHEMERAL_CONTAINER = "pyracast-postgres-ephemeral"
VOLUME = "pyracast-postgres-data"
# Only for throwaway clusters: a crash can lose or corrupt data that was not flushed.
NON_DURABLE_OPTIONS = (
    "-c",
    "fsync=off",
    "-c",
    "synchronous_commit=off",
    "-c",
    "full_page_writes=off",
)


def execute(
    command: list[str] | tuple[str, ...], **options: Any
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, **options)  # noqa: S603 -- fixed commands and local paths.


def run(*args: str, env: dict[str, str] | None = None) -> None:
    execute(args, check=True, env=env)


def durable_root() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ["LOCALAPPDATA"])
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "pyracast"


def require_durable(path: Path, temporary: Path | None = None) -> None:
    temporary = Path(tempfile.gettempdir()) if temporary is None else temporary
    if path.resolve().is_relative_to(temporary.resolve()):
        raise RuntimeError(
            f"Development data must not live under the temporary directory {temporary}; "
            "OS cleanup deletes it."
        )


def grant_current_user(root: Path) -> None:
    system = Path(os.environ["SYSTEMROOT"]) / "System32"
    identity = execute(
        [str(system / "whoami.exe"), "/user", "/fo", "csv", "/nh"],
        check=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    ).stdout
    sid = next(csv.reader(identity.splitlines()))[1]
    if re.fullmatch(r"S-\d+(?:-\d+)+", sid) is None:
        raise RuntimeError("Windows user SID could not be determined")
    # initdb drops Administrators; an elevated owner of the directory may be that group.
    run(str(system / "icacls.exe"), str(root), "/grant", f"*{sid}:(OI)(CI)F", "/Q")


def windows_workspace() -> Path:
    root = Path(tempfile.mkdtemp(prefix="pyracast-postgres-"))
    grant_current_user(root)
    return root


def windows_preflight(binary: Path, env: dict[str, str]) -> None:
    try:
        for executable in ("initdb.exe", "postgres.exe"):
            run(str(binary / executable), "--version", env=env)
    except subprocess.CalledProcessError as error:
        print(f"PostgreSQL native preflight failed: 0x{error.returncode:08X}", flush=True)
        if sys.platform == "win32":
            import ctypes

            with (
                os.add_dll_directory(str(binary)),
                os.add_dll_directory(str(binary.parent / "lib")),
            ):
                for name in (
                    "VCRUNTIME140.dll",
                    "VCRUNTIME140_1.dll",
                    "MSVCP140.dll",
                    "libwinpthread-1.dll",
                    "libiconv-2.dll",
                    "libintl-9.dll",
                    "icuuc77.dll",
                    "icuin77.dll",
                    "libcrypto-3-x64.dll",
                    "libssl-3-x64.dll",
                    "libpq.dll",
                    "libxml2.dll",
                    "liblz4.dll",
                    "libzstd.dll",
                ):
                    path = binary / name
                    try:
                        ctypes.WinDLL(str(path) if path.is_file() else name)
                        print(f"Native library probe: {name}: loadable", flush=True)
                    except OSError as failure:
                        print(
                            f"Native library probe: {name}: Windows error {failure.winerror}",
                            flush=True,
                        )
        raise


def accepts_connections(port: str) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", int(port))) == 0


def install_binaries(root: Path, *, throwaway: bool = False) -> Path:
    binary = root / "pgsql" / "bin"
    if (binary / "pg_ctl.exe").is_file():
        return binary
    root.mkdir(parents=True, exist_ok=True)
    digest = THROWAWAY_ARCHIVE_SHA256 if throwaway else ARCHIVE_SHA256
    local_archive = os.environ.get("PYRACAST_POSTGRES_ARCHIVE")
    archive = Path(local_archive) if local_archive else root / "postgres.zip"
    if not local_archive and throwaway:
        urllib.request.urlretrieve(THROWAWAY_ARCHIVE_URL, archive)
    elif not local_archive:
        urllib.request.urlretrieve(ARCHIVE_URL, archive)
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
            raise RuntimeError("PostgreSQL archive checksum mismatch")
    with zipfile.ZipFile(archive) as source:
        for item in source.namelist():
            if item.startswith(("pgsql/bin/", "pgsql/lib/", "pgsql/share/")):
                source.extract(item, root)
    if not local_archive:
        archive.unlink()
    return binary


def start_server(command: list[str], env: dict[str, str]) -> None:
    # pg_ctl's server inherits the caller's handles; a pipe would keep the caller waiting.
    execute(
        command,
        env=env,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=90,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def database_exists(binary: Path, port: str, env: dict[str, str]) -> bool:
    found = execute(
        [
            str(binary / "psql.exe"),
            "-h",
            "127.0.0.1",
            "-p",
            port,
            "-U",
            "pyracast",
            "-d",
            "postgres",
            "-w",
            "-X",
            "-tAc",
            "SELECT 1 FROM pg_database WHERE datname = 'pyracast'",
        ],
        env=env,
        check=True,
        capture_output=True,
    ).stdout
    return found.strip() == "1"


def provision_windows(
    root: Path, port: str, options: tuple[str, ...], *, throwaway: bool = False
) -> str:
    data = root / "data"
    cluster = (data / "PG_VERSION").is_file()
    if not cluster and data.exists() and any(data.iterdir()):
        raise RuntimeError(f"{data} is not a PostgreSQL cluster; nothing was changed.")
    binary = install_binaries(root, throwaway=throwaway)
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join((str(binary), str(binary.parent / "lib"), env.get("PATH", "")))
    if cluster and accepts_connections(port):
        status = "running"
    else:
        if not cluster:
            windows_preflight(binary, env)
            run(
                str(binary / "initdb.exe"),
                "-D",
                str(data),
                "-U",
                "pyracast",
                "--auth=trust",
                "--encoding=UTF8",
                "--no-locale",
                env=env,
            )
        server = " ".join(("-h", "127.0.0.1", "-p", port, *options))
        log = root / "postgres.log"
        pg_ctl = str(binary / "pg_ctl.exe")
        start_server(
            [pg_ctl, "-D", str(data), "-l", str(log), "-o", server, "-w", "-t", "60", "start"], env
        )
        status = "started" if cluster else "created"
    verify_windows_server(binary, data, port, env)
    if not database_exists(binary, port, env):
        run(
            str(binary / "createdb.exe"),
            "-h",
            "127.0.0.1",
            "-p",
            port,
            "-U",
            "pyracast",
            "pyracast",
            env=env,
        )
    return status


def verify_windows_server(binary: Path, data: Path, port: str, env: dict[str, str]) -> None:
    connection = ["-h", "127.0.0.1", "-p", port, "-U", "pyracast", "-d", "postgres"]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        ready = execute(
            [str(binary / "pg_isready.exe"), *connection, "-t", "1"],
            env=env,
            check=False,
            capture_output=True,
            timeout=5,
        )
        if ready.returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError("PostgreSQL did not become ready; no database was changed.")
    identity = execute(
        [str(binary / "psql.exe"), *connection, "-w", "-X", "-tAc", "SHOW data_directory"],
        env={**env, "PGCONNECT_TIMEOUT": "3"},
        check=True,
        capture_output=True,
        timeout=5,
    ).stdout.strip()
    if Path(identity).resolve() != data.resolve():
        raise RuntimeError(
            f"Port {port} belongs to a different PostgreSQL cluster; no database was changed."
        )


@dataclass(frozen=True)
class Container:
    running: bool
    volumes: frozenset[str]
    command: tuple[str, ...]
    ports: frozenset[tuple[str, str]]
    data_directory: PurePosixPath


def inspect_container(docker: str, name: str) -> Container | None:
    result = execute([docker, "container", "inspect", name], capture_output=True, check=False)
    if result.returncode:
        return None
    (details,) = json.loads(result.stdout)
    mounts = details.get("Mounts") or []
    bindings = (details.get("HostConfig") or {}).get("PortBindings") or {}
    pgdata = next(
        (
            value.removeprefix("PGDATA=")
            for value in details["Config"].get("Env") or ()
            if value.startswith("PGDATA=")
        ),
        "/var/lib/postgresql/18/docker",
    )
    return Container(
        running=details["State"]["Running"] is True,
        volumes=frozenset(
            mount["Name"]
            for mount in mounts
            if mount.get("Type") == "volume"
            and mount.get("Destination") == "/var/lib/postgresql"
            and mount.get("RW") is True
        ),
        command=tuple(details["Config"].get("Cmd") or ()),
        ports=frozenset(
            (binding["HostIp"], binding["HostPort"]) for binding in (bindings.get("5432/tcp") or [])
        ),
        data_directory=PurePosixPath(posixpath.normpath(pgdata)),
    )


def wait_for_container(docker: str, name: str) -> None:
    for _ in range(60):
        if (
            execute(
                (docker, "exec", name, "pg_isready", "-U", "pyracast"),
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        ):
            return
        time.sleep(1)
    raise RuntimeError("PostgreSQL did not become ready")


def production_image() -> str:
    found = re.search(
        r"^FROM (postgres:\S+@sha256:[0-9a-f]{64})$",
        PRODUCTION_DOCKERFILE.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if found is None:
        raise RuntimeError(f"{PRODUCTION_DOCKERFILE} names no pinned PostgreSQL image.")
    return found[1]


def provision_docker(
    docker: str, port: str, options: tuple[str, ...], *, volume: str | None
) -> str:
    name = CONTAINER if volume else f"{EPHEMERAL_CONTAINER}-{uuid4().hex}"
    existing = inspect_container(docker, name) if volume else None
    if existing is not None and (
        volume not in existing.volumes
        or not existing.data_directory.is_relative_to("/var/lib/postgresql")
        or not frozenset(NON_DURABLE_OPTIONS[1::2]).isdisjoint(existing.command)
    ):
        raise RuntimeError(
            f"Container {name} does not keep durable data in volume {volume}; nothing was "
            "changed. Move its data or remove it, then rerun."
        )
    if existing is not None and existing.ports != frozenset({("127.0.0.1", port)}):
        raise RuntimeError(
            f"Container {name} does not publish only loopback port {port}; nothing was changed."
        )
    if existing is not None and existing.running:
        wait_for_container(docker, name)
        return "running"
    if existing is not None:
        run(docker, "start", name)
        status = "started"
    else:
        storage = ("--volume", f"{volume}:/var/lib/postgresql") if volume else ()
        run(
            docker,
            "run",
            "--detach",
            "--name",
            name,
            "-p",
            f"127.0.0.1:{port}:5432",
            *storage,
            "-e",
            "POSTGRES_USER=pyracast",
            "-e",
            "POSTGRES_DB=pyracast",
            "-e",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            POSTGRES_IMAGE if volume else production_image(),
            *(("postgres", *options) if options else ()),
        )
        status = "created"
    wait_for_container(docker, name)
    return status


def parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start the development PostgreSQL; it keeps its data in the per-user data "
        "directory unless --ephemeral is given."
    )
    parser.add_argument("--ephemeral", action="store_true", help="create a throwaway cluster (CI)")
    parser.add_argument(
        "--non-durable",
        action="store_true",
        help="turn off fsync, synchronous_commit and full_page_writes; needs --ephemeral",
    )
    parser.add_argument(
        "--port",
        default=os.environ.get("PGPORT", "55432"),
        help="listening port; defaults to PGPORT, else 55432",
    )
    arguments = parser.parse_args(argv)
    if arguments.non_durable and not arguments.ephemeral:
        parser.error("--non-durable is only for a throwaway cluster; add --ephemeral")
    try:
        arguments.port = int(arguments.port)
    except ValueError:
        parser.error("port must be an integer between 1 and 65535")
    if not 1 <= arguments.port <= 65535:
        parser.error("port must be between 1 and 65535")
    return arguments


def main(argv: list[str] | None = None) -> None:
    arguments = parse(argv)
    options = NON_DURABLE_OPTIONS if arguments.non_durable else ()
    port = str(arguments.port)
    if sys.platform == "win32":
        if arguments.ephemeral:
            root = windows_workspace()
        else:
            root = durable_root() / "postgres"
            require_durable(root)
            if not root.exists():
                root.mkdir(parents=True)
                grant_current_user(root)
        status = provision_windows(root, port, options, throwaway=arguments.ephemeral)
        where = str(root)
    elif sys.platform == "linux":
        docker = shutil.which("docker")
        if docker is None:
            raise RuntimeError("Docker is required on Linux")
        volume = None if arguments.ephemeral else VOLUME
        status = provision_docker(docker, port, options, volume=volume)
        where = f"Docker volume {volume}" if volume else "a throwaway container"
    else:
        raise RuntimeError(f"Unsupported platform: {sys.platform}")
    with socket.create_connection(("127.0.0.1", int(port)), timeout=10):
        pass
    print(f"PostgreSQL {status} on 127.0.0.1:{port}; data in {where}.", flush=True)


if __name__ == "__main__":
    main()
