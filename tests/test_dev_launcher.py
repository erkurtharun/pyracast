import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import psutil
import pytest

from scripts import dev as launcher


def wait_for_marker(path: Path) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if path.is_file() and path.stat().st_size:
            return
        time.sleep(0.02)
    raise AssertionError(f"Test process did not create {path.name}")


@pytest.fixture
def fake_processes() -> Iterator[Callable[[Path, str], subprocess.Popen[bytes]]]:
    children: list[tuple[subprocess.Popen[bytes], list[psutil.Process]]] = []

    def spawn(root: Path, role: str) -> subprocess.Popen[bytes]:
        root.mkdir(exist_ok=True)
        (root / "django.py").write_text(
            "import os\n"
            "import sys\n"
            "import threading\n"
            "from pathlib import Path\n"
            "Path('ready-' + sys.argv[1]).write_text(str(os.getpid()))\n"
            "threading.Event().wait()\n",
            encoding="utf-8",
        )
        env = {**os.environ, "PYTHONPATH": str(root)}
        process = subprocess.Popen(  # noqa: S603 -- inert test module in an isolated directory.
            [sys.executable, "-m", "django", role],
            cwd=root,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        identities = [psutil.Process(process.pid)]
        children.append((process, identities))
        wait_for_marker(root / f"ready-{role}")
        actual = psutil.Process(int((root / f"ready-{role}").read_text()))
        if actual.pid != process.pid:
            identities.append(actual)
        return process

    yield spawn

    for child, identities in reversed(children):
        for process in reversed(identities):
            with suppress(psutil.NoSuchProcess):
                process.terminate()
        child.wait(timeout=5)


def test_discovers_records_and_stops_the_exact_workspace_process(
    tmp_path: Path,
    fake_processes: Callable[[Path, str], subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path.resolve()
    child = fake_processes(root, "worker")
    (root / ".dev").mkdir()
    monkeypatch.setattr(launcher, "wait_for_jobs", lambda: None)

    process = launcher.discover(root, "worker")
    assert process is not None
    assert process.pid == int((root / "ready-worker").read_text())
    record = launcher.identity(process)
    state = {"root": str(root), "processes": {"worker": record}}
    launcher.save_state(root, state)
    restored = launcher.read_state(root)
    assert launcher.recorded_process(root, "worker", restored["processes"]["worker"]) == process

    launcher.stop(root, restored)

    child.wait(timeout=5)
    assert child.poll() is not None
    assert launcher.read_state(root)["processes"] == {}


def test_stale_pid_identity_does_not_stop_the_live_process(
    tmp_path: Path,
    fake_processes: Callable[[Path, str], subprocess.Popen[bytes]],
) -> None:
    root = tmp_path.resolve()
    child = fake_processes(root, "worker")
    (root / ".dev").mkdir()
    actual = psutil.Process(int((root / "ready-worker").read_text()))
    record = launcher.identity(actual)
    record["created"] -= 100
    state = {"root": str(root), "processes": {"worker": record}}

    launcher.stop(root, state)

    assert child.poll() is None
    assert launcher.recorded_process(root, "worker", record) is None
    assert launcher.read_state(root)["processes"] == {}


def test_foreign_workspace_worker_is_never_adopted(
    tmp_path: Path,
    fake_processes: Callable[[Path, str], subprocess.Popen[bytes]],
) -> None:
    foreign = tmp_path / "another-workspace"
    child = fake_processes(foreign, "worker")
    root = tmp_path / "this-workspace"
    root.mkdir()

    assert launcher.discover(root.resolve(), "worker") is None
    actual = psutil.Process(int((foreign / "ready-worker").read_text()))
    assert launcher.recorded_process(root.resolve(), "worker", launcher.identity(actual)) is None
    assert child.poll() is None


def test_command_lock_excludes_another_os_process(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from scripts.dev import command_lock\n"
        "try:\n"
        "    with command_lock(Path(sys.argv[1])):\n"
        "        raise SystemExit(2)\n"
        "except RuntimeError:\n"
        "    print('locked')\n"
        "    raise SystemExit(7)\n"
    )
    with launcher.command_lock(tmp_path):
        result = subprocess.run(  # noqa: S603 -- fixed isolated lock probe.
            [sys.executable, "-c", code, str(tmp_path)],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

    assert result.returncode == 7
    assert result.stdout.strip() == "locked"
    with launcher.command_lock(tmp_path):
        pass


def test_state_from_another_workspace_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / ".dev"
    directory.mkdir()
    (directory / "state.json").write_text(
        '{"root": "another-workspace", "processes": {}}', encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="another workspace"):
        launcher.read_state(tmp_path)


def test_running_frontend_dependencies_are_not_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = tmp_path / "web"
    web.mkdir()
    (tmp_path / ".dev").mkdir()
    lock = web / "package-lock.json"
    lock.write_text('{"lockfileVersion":3}', encoding="utf-8")
    npm = tmp_path / "npm-cli.js"
    npm.touch()
    monkeypatch.setattr(shutil, "which", lambda name: str(npm))
    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs: "v26.13.0")
    setup = MagicMock()
    monkeypatch.setattr(launcher, "run_setup", setup)

    with pytest.raises(RuntimeError, match="dependencies changed"):
        launcher.frontend_command(tmp_path, running=True)

    setup.assert_not_called()
    assert lock.read_text(encoding="utf-8") == '{"lockfileVersion":3}'
    assert not (tmp_path / ".dev/npm-lock.sha256").exists()


def test_foreign_port_collision_fails_before_setup_or_dependency_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path.resolve()
    dependencies = MagicMock()
    setup = MagicMock()
    monkeypatch.setattr(launcher, "ensure_dependencies", dependencies)
    monkeypatch.setattr(launcher, "run_setup", setup)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        monkeypatch.setattr(launcher, "PORTS", {"api": port})

        with pytest.raises(RuntimeError, match=f"Port {port} belongs to another process"):
            launcher.start(root, {"root": str(root), "processes": {}})

    dependencies.assert_not_called()
    setup.assert_not_called()
    assert not (root / ".dev").exists()


def test_failed_frontend_start_rolls_back_new_api_and_preserves_existing_worker(
    tmp_path: Path,
    fake_processes: Callable[[Path, str], subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path.resolve()
    existing = fake_processes(root, "worker")
    (root / ".dev").mkdir()
    (root / "web").mkdir()
    monkeypatch.setenv("PYTHONPATH", str(root))
    monkeypatch.setattr(launcher, "PORTS", {})
    monkeypatch.setattr(launcher, "ensure_dependencies", lambda state: None)
    monkeypatch.setattr(launcher, "run_setup", lambda root, args: None)
    monkeypatch.setattr(
        launcher,
        "frontend_command",
        lambda root, **kwargs: [sys.executable, "-m", "django", "frontend"],
    )
    launched: list[psutil.Process] = []

    def ready(process: psutil.Process, role: str) -> None:
        launched.append(process)
        marker = root / "ready-runserver" if role == "api" else root / "web/ready-frontend"
        wait_for_marker(marker)
        if role == "frontend":
            raise RuntimeError("frontend test startup failure")

    monkeypatch.setattr(launcher, "wait_ready", ready)
    state: dict[str, Any] = {"root": str(root), "processes": {}}

    with pytest.raises(RuntimeError, match="frontend test startup failure"):
        launcher.start(root, state)

    assert len(launched) == 2
    assert all(not process.is_running() for process in launched)
    assert existing.poll() is None
    assert launcher.read_state(root)["processes"] == {}
