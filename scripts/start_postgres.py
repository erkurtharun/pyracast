import argparse
import csv
import hashlib
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

ARCHIVE_URL = "https://sbp.enterprisedb.com/getfile.jsp?fileid=1260609"
ARCHIVE_SHA256 = "e2246ba91d22345bc3d017586c09ede52d9df180b1eeb480f050445f1cad84e2"
POSTGRES_IMAGE = (
    "postgres:18.6@sha256:e3d24f6906d0760bf4da0dae872415391ededefad23fd471c47dc53b0ddf3c42"
)
# Only for throwaway clusters: a crash can lose or corrupt data that was not flushed.
NON_DURABLE_OPTIONS = (
    "-c",
    "fsync=off",
    "-c",
    "synchronous_commit=off",
    "-c",
    "full_page_writes=off",
)


def run(*args: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(args, check=True, env=env)  # noqa: S603 -- fixed CI commands and local paths.


def windows_workspace() -> Path:
    root = Path(tempfile.mkdtemp(prefix="pyracast-postgres-"))
    system = Path(os.environ["SYSTEMROOT"]) / "System32"
    identity = subprocess.check_output(  # noqa: S603 -- fixed Windows identity query.
        [str(system / "whoami.exe"), "/user", "/fo", "csv", "/nh"],
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    sid = next(csv.reader(identity.splitlines()))[1]
    if re.fullmatch(r"S-\d+(?:-\d+)+", sid) is None:
        raise RuntimeError("Windows user SID could not be determined")
    # initdb drops Administrators; an elevated mkdtemp owner may be that group, not the user.
    run(str(system / "icacls.exe"), str(root), "/grant", f"*{sid}:(OI)(CI)F", "/Q")
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


def server_options(argv: list[str] | None) -> tuple[str, ...]:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--non-durable",
        action="store_true",
        help="turn off fsync, synchronous_commit and full_page_writes on a throwaway cluster",
    )
    return NON_DURABLE_OPTIONS if parser.parse_args(argv).non_durable else ()


def main(argv: list[str] | None = None) -> None:
    options = server_options(argv)
    port = str(int(os.environ.get("PGPORT", "55432")))
    if sys.platform == "win32":
        root = windows_workspace()
        archive = root / "postgres.zip"
        local_archive = os.environ.get("PYRACAST_POSTGRES_ARCHIVE")
        if local_archive:
            archive = Path(local_archive)
        else:
            urllib.request.urlretrieve(ARCHIVE_URL, archive)
        with archive.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != ARCHIVE_SHA256:
                raise RuntimeError("PostgreSQL archive checksum mismatch")
        with zipfile.ZipFile(archive) as source:
            for item in source.namelist():
                if item.startswith(("pgsql/bin/", "pgsql/lib/", "pgsql/share/")):
                    source.extract(item, root)
        binary = root / "pgsql" / "bin"
        env = os.environ.copy()
        env["PATH"] = os.pathsep.join(
            (str(binary), str(binary.parent / "lib"), env.get("PATH", ""))
        )
        windows_preflight(binary, env)
        data = root / "data"
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
        run(
            str(binary / "pg_ctl.exe"),
            "-D",
            str(data),
            "-l",
            str(root / "postgres.log"),
            "-o",
            " ".join(("-h", "127.0.0.1", "-p", port, *options)),
            "start",
            env=env,
        )
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
    elif sys.platform == "linux":
        docker = shutil.which("docker")
        if docker is None:
            raise RuntimeError("Docker is required on Linux")
        run(
            docker,
            "run",
            "--detach",
            "--name",
            "pyracast-postgres",
            "-p",
            f"127.0.0.1:{port}:5432",
            "-e",
            "POSTGRES_USER=pyracast",
            "-e",
            "POSTGRES_DB=pyracast",
            "-e",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            POSTGRES_IMAGE,
            *(("postgres", *options) if options else ()),
        )
        for _ in range(60):
            if (
                subprocess.run(  # noqa: S603 -- fixed Docker command.
                    (docker, "exec", "pyracast-postgres", "pg_isready", "-U", "pyracast"),
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ).returncode
                == 0
            ):
                break
            time.sleep(1)
        else:
            raise RuntimeError("PostgreSQL did not become ready")
    else:
        raise RuntimeError(f"Unsupported platform: {sys.platform}")
    with socket.create_connection(("127.0.0.1", int(port)), timeout=10):
        pass


if __name__ == "__main__":
    main()
