import hashlib
import os
import platform
import socket
import subprocess
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

ERLANG_URL = "https://github.com/erlang/otp/releases/download/OTP-29.1.2/otp_win64_29.1.2.zip"
ERLANG_SHA256 = "4efe9772a84c6c1ef75377e9326815584958776c905f2bf687b62509e2d809c0"
RABBITMQ_URL = (
    "https://github.com/rabbitmq/rabbitmq-server/releases/download/v4.4.0/"
    "rabbitmq-server-windows-4.4.0.zip"
)
RABBITMQ_SHA256 = "c077a084890e57e1498e479913d4d5bda77e88647e153b433e97f72a31431542"
QUEUE_URL = "amqp://guest:guest@127.0.0.1:5672/%2F"


def download_and_extract(url: str, digest: str, archive: Path, destination: Path) -> None:
    urllib.request.urlretrieve(url, archive)  # noqa: S310 -- fixed official HTTPS URLs.
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
            raise RuntimeError("RabbitMQ dependency archive checksum mismatch")
    with zipfile.ZipFile(archive) as source:
        source.extractall(destination)


def port_open() -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", 5672)) == 0


def main() -> None:
    if platform.system() != "Windows":
        raise RuntimeError("This RabbitMQ bootstrap is for Windows")
    if port_open():
        raise RuntimeError("RabbitMQ port 5672 is already in use")
    root = Path(tempfile.mkdtemp(prefix="pyracast-rabbitmq-"))
    erlang = root / "erlang"
    download_and_extract(ERLANG_URL, ERLANG_SHA256, root / "erlang.zip", erlang)
    download_and_extract(RABBITMQ_URL, RABBITMQ_SHA256, root / "rabbitmq.zip", root)
    env = os.environ.copy()
    env["ERLANG_HOME"] = str(erlang)
    env["RABBITMQ_BASE"] = str(root / "state")
    env["RABBITMQ_NODENAME"] = "pyracastci@localhost"
    config = root / "rabbitmq.conf"
    config.write_text("listeners.tcp.1 = 127.0.0.1:5672\n", encoding="utf-8")
    env["RABBITMQ_CONFIG_FILE"] = str(config)
    server = root / "rabbitmq_server-4.4.0" / "sbin" / "rabbitmq-server.bat"
    log = root / "broker.log"
    with log.open("w", encoding="utf-8") as output:
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
            github_env = os.environ.get("GITHUB_ENV")
            if github_env:
                with Path(github_env).open("a", encoding="utf-8") as output:
                    output.write(f"PYRACAST_LIVE_QUEUE=1\nPYRACAST_RABBITMQ_URL={QUEUE_URL}\n")
            print(f"RabbitMQ ready at {QUEUE_URL}; state and log: {root}")
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


if __name__ == "__main__":
    main()
