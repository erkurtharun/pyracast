import hashlib
import os
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


def run(*args: str) -> None:
    subprocess.run(args, check=True)  # noqa: S603 -- fixed CI commands and local paths.


def main() -> None:
    port = str(int(os.environ.get("PGPORT", "55432")))
    if sys.platform == "win32":
        root = Path(tempfile.mkdtemp(prefix="pyracast-postgres-"))
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
        )
        run(
            str(binary / "pg_ctl.exe"),
            "-D",
            str(data),
            "-l",
            str(root / "postgres.log"),
            "-o",
            f"-h 127.0.0.1 -p {port}",
            "start",
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
