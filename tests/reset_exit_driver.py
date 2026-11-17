"""Child process of the reset tool's exit-status test, run with the working directory as host root.

`abort` fails its readiness probe while the reader of stdout goes away; with
RESET_EXIT_SIGNAL=SIGINT a real SIGINT also arrives during the first stop command.
"""

import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from deploy import reset_database


def main() -> None:
    root = Path.cwd()
    deliver = os.environ["RESET_EXIT_SIGNAL"] == "SIGINT"
    reader, writer = os.pipe()
    os.dup2(writer, sys.stdout.fileno())
    os.close(writer)
    host = reset_database.Host(
        config=root / "host.json",
        release=root,
        bucket="private-backups",
        runtime_version="7",
        marker=root / "release-blocked",
        lock=root / "release.lock",
        state=root / "reset",
        environment=root,
    )
    host.marker.write_text("maintenance")
    (root / "deploy").mkdir()
    (root / "deploy/release.env").write_text("SITE_ADDRESS=example.com\n")

    def run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[0] == "curl":
            os.close(reader)
            return subprocess.CompletedProcess(command, 22, "", "")
        if deliver and "stop" in command and "gateway" in command:
            signal.raise_signal(signal.SIGINT)
        return subprocess.CompletedProcess(command, 0, "", "")

    lock = SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=lambda *_arguments: None)
    with (
        patch.object(subprocess, "run", run),
        patch.object(reset_database, "load_host", lambda _path: host),
        patch.object(reset_database, "importlib", SimpleNamespace(import_module=lambda _: lock)),
    ):
        status = reset_database.main([str(host.config), "abort"])
    raise SystemExit(status)


if __name__ == "__main__":
    main()
