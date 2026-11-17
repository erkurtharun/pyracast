import argparse
import hashlib
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

from scripts.start_postgres import durable_root, require_durable

ERLANG_URL = "https://github.com/erlang/otp/releases/download/OTP-29.1.2/otp_win64_29.1.2.zip"
ERLANG_SHA256 = "4efe9772a84c6c1ef75377e9326815584958776c905f2bf687b62509e2d809c0"
RABBITMQ_URL = (
    "https://github.com/rabbitmq/rabbitmq-server/releases/download/v4.4.0/"
    "rabbitmq-server-windows-4.4.0.zip"
)
RABBITMQ_SHA256 = "c077a084890e57e1498e479913d4d5bda77e88647e153b433e97f72a31431542"
PORT = 5672
QUEUE_URL = f"amqp://guest:guest@127.0.0.1:{PORT}/%2F"
SERVER = Path("rabbitmq_server-4.4.0") / "sbin" / "rabbitmq-server.bat"


def download_and_extract(url: str, digest: str, archive: Path, destination: Path) -> None:
    urllib.request.urlretrieve(url, archive)  # noqa: S310 -- fixed official HTTPS URLs.
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
            raise RuntimeError("RabbitMQ dependency archive checksum mismatch")
    with zipfile.ZipFile(archive) as source:
        source.extractall(destination)
    archive.unlink()


def port_open() -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", PORT)) == 0


def start_broker(root: Path, server: Path) -> None:
    env = os.environ.copy()
    env["ERLANG_HOME"] = str(root / "erlang")
    env["RABBITMQ_BASE"] = str(root / "state")
    env["RABBITMQ_NODENAME"] = "pyracastci@localhost"
    env["RABBITMQ_CONFIG_FILE"] = str(root / "rabbitmq.conf")
    log = root / "broker.log"
    with log.open("a", encoding="utf-8") as output:
        process = subprocess.Popen(  # noqa: S603 -- fixed downloaded server path.
            [os.environ["COMSPEC"], "/c", str(server)],
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"RabbitMQ exited before readiness; see {log}")
        if port_open():
            return
        time.sleep(1)
    subprocess.run(  # noqa: S603 -- taskkill targets only this process tree.
        [
            str(Path(os.environ["SYSTEMROOT"]) / "System32" / "taskkill.exe"),
            "/PID",
            str(process.pid),
            "/T",
            "/F",
        ],
        check=False,
        timeout=15,
        capture_output=True,
    )
    raise RuntimeError(f"RabbitMQ did not become ready; see {log}")


def provision_windows(root: Path) -> str:
    if port_open():
        return "running"
    root.mkdir(parents=True, exist_ok=True)
    erlang = root / "erlang"
    if not (erlang / "bin").is_dir():
        download_and_extract(ERLANG_URL, ERLANG_SHA256, root / "erlang.zip", erlang)
    if not (root / SERVER).is_file():
        download_and_extract(RABBITMQ_URL, RABBITMQ_SHA256, root / "rabbitmq.zip", root)
    config = root / "rabbitmq.conf"
    if not config.is_file():
        config.write_text(f"listeners.tcp.1 = 127.0.0.1:{PORT}\n", encoding="utf-8")
    start_broker(root, root / SERVER)
    return "started"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Start the Windows development RabbitMQ; it keeps its state in the per-user "
        "data directory unless --ephemeral is given."
    )
    parser.add_argument("--ephemeral", action="store_true", help="use a throwaway broker (CI)")
    ephemeral = parser.parse_args(argv).ephemeral
    if sys.platform != "win32":
        raise RuntimeError("This RabbitMQ bootstrap is for Windows; see README for Docker.")
    if ephemeral:
        root = Path(tempfile.mkdtemp(prefix="pyracast-rabbitmq-"))
    else:
        root = durable_root() / "rabbitmq"
        require_durable(root)
    status = provision_windows(root)
    github_env = os.environ.get("GITHUB_ENV")
    if github_env:
        with Path(github_env).open("a", encoding="utf-8") as output:
            output.write(f"PYRACAST_LIVE_QUEUE=1\nPYRACAST_RABBITMQ_URL={QUEUE_URL}\n")
    print(f"RabbitMQ {status} at {QUEUE_URL}; state and log: {root}", flush=True)


if __name__ == "__main__":
    main()
