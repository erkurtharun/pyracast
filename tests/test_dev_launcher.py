import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
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


@contextmanager
def cooperative_process(
    root: Path, role: str, *, acknowledge: bool = True
) -> Iterator[psutil.Process]:
    (root / ".dev").mkdir(exist_ok=True)
    (root / "django.py").write_text(
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "stop = Path(sys.argv[sys.argv.index('--stop-file') + 1])\n"
        "Path('ready').write_text(str(os.getpid()))\n"
        f"while {not acknowledge!r} or not stop.exists():\n"
        "    time.sleep(0.01)\n"
        "Path('finished').write_text('completed')\n",
        encoding="utf-8",
    )
    child = subprocess.Popen(  # noqa: S603 -- inert cooperative process in an isolated directory.
        [
            sys.executable,
            "-m",
            "django",
            "dispatch" if role == "dispatcher" else "worker" if role == "mailer" else role,
            *(["--queues", "notifications"] if role == "mailer" else []),
            "--stop-file",
            str(root / ".dev" / f"{role}.stop"),
        ],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        wait_for_marker(root / "ready")
        process = launcher.discover(root, role)
        assert process is not None
        yield process
    finally:
        if child.poll() is None:
            launcher.terminate_tree(psutil.Process(child.pid))
        child.wait(timeout=5)


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
    actual = psutil.Process(int((root / "ready-worker").read_text()))
    outer = psutil.Process(child.pid)
    expected = outer if launcher.process_role(outer, root) == "worker" else actual
    assert process.pid == expected.pid
    assert actual.pid in {process.pid, *(p.pid for p in process.children(recursive=True))}
    record = launcher.identity(process)
    state = {"root": str(root), "processes": {"worker": record}}
    launcher.save_state(root, state)
    restored = launcher.read_state(root)
    assert launcher.recorded_process(root, "worker", restored["processes"]["worker"]) == process

    launcher.stop(root, restored)

    child.wait(timeout=5)
    assert child.poll() is not None
    _, alive = psutil.wait_procs([actual], timeout=5)
    assert alive == []
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


@pytest.mark.parametrize("role", ["worker", "mailer", "dispatcher"])
def test_launcher_waits_for_cooperative_worker_exit(tmp_path: Path, role: str) -> None:
    root = tmp_path.resolve()
    with cooperative_process(root, role) as process:
        state = {"root": str(root), "processes": {role: launcher.identity(process)}}
        launcher.save_state(root, state)

        launcher.stop(root, state)

        assert (root / "finished").read_text(encoding="utf-8") == "completed"
        assert launcher.read_state(root)["processes"] == {}


def test_launcher_preserves_worker_identity_when_cooperative_stop_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path.resolve()
    stop_cooperatively = launcher.stop_cooperatively
    monkeypatch.setattr(
        launcher,
        "stop_cooperatively",
        lambda root, role, process: stop_cooperatively(root, role, process, timeout=0),
    )
    with cooperative_process(root, "worker", acknowledge=False) as process:
        record = launcher.identity(process)
        state = {"root": str(root), "processes": {"worker": record}}
        launcher.save_state(root, state)

        with pytest.raises(RuntimeError, match="left running"):
            launcher.stop(root, state)

        assert process.is_running()
        assert (root / ".dev/worker.stop").exists()
        assert launcher.read_state(root)["processes"]["worker"] == record


def test_cooperative_stop_rejects_foreign_marker_path(tmp_path: Path) -> None:
    process = MagicMock()
    process.cmdline.return_value = ["python", "--stop-file", str(tmp_path / "foreign.stop")]

    assert not launcher.stop_cooperatively(tmp_path, "worker", process)
    assert not (tmp_path / "foreign.stop").exists()
    process.wait.assert_not_called()


@pytest.mark.parametrize("role", ["worker", "dispatcher"])
def test_start_refuses_to_adopt_process_with_pending_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    root = tmp_path.resolve()
    dependencies = MagicMock(side_effect=AssertionError("Shutdown preflight must run first"))
    monkeypatch.setattr(launcher, "ensure_dependencies", dependencies)
    with cooperative_process(root, role, acknowledge=False) as process:
        stop_file = root / ".dev" / f"{role}.stop"
        stop_file.touch()
        record = launcher.identity(process)
        state: dict[str, Any] = {"root": str(root), "processes": {role: record}}

        with pytest.raises(RuntimeError, match=f"{role} is still shutting down"):
            launcher.start(root, state)

        dependencies.assert_not_called()
        assert process.is_running()
        assert stop_file.exists()
        assert state["processes"][role] == record


@pytest.mark.django_db(transaction=True)
def test_worker_drain_waits_for_active_notification(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_notifications import ready

    delivery = ready()
    delivery.status = "sending"
    delivery.save(update_fields=["status"])

    def finish_send(_: float) -> None:
        delivery.status = "sent"
        delivery.save(update_fields=["status"])

    monkeypatch.setattr(time, "sleep", finish_send)

    launcher.wait_for_jobs()

    delivery.refresh_from_db()
    assert delivery.status == "sent"


@pytest.mark.django_db(transaction=True)
def test_worker_drain_timeout_preserves_active_notification() -> None:
    from tests.test_notifications import ready

    delivery = ready()
    delivery.status = "sending"
    delivery.save(update_fields=["status"])

    with pytest.raises(RuntimeError, match="worker was left running"):
        launcher.wait_for_jobs(timeout=0)

    delivery.refresh_from_db()
    assert delivery.status == "sending"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("status", ["pending", "retry", "uncertain"])
def test_worker_drain_does_not_wait_for_inactive_notification(status: str) -> None:
    from tests.test_notifications import ready

    delivery = ready()
    delivery.status = status
    delivery.save(update_fields=["status"])

    launcher.wait_for_jobs(timeout=0)

    delivery.refresh_from_db()
    assert delivery.status == status


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


@pytest.mark.parametrize(
    ("acknowledge", "failure_type", "cleanup_type"),
    [
        (True, RuntimeError, None),
        (False, RuntimeError, None),
        (False, KeyboardInterrupt, None),
        (False, SystemExit, None),
        (True, RuntimeError, PermissionError),
        (True, RuntimeError, psutil.AccessDenied),
    ],
)
def test_failed_dispatcher_start_requests_worker_shutdown_and_preserves_timeout_state(
    tmp_path: Path,
    fake_processes: Callable[[Path, str], subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
    acknowledge: bool,
    failure_type: type[BaseException],
    cleanup_type: type[Exception] | None,
) -> None:
    root = tmp_path.resolve()
    api = fake_processes(root, "runserver")
    frontend = fake_processes(root / "web", "frontend")
    (root / ".dev").mkdir()
    for role in ("worker", "mailer", "dispatcher"):
        (root / ".dev" / f"{role}.stop").touch()
    (root / "django.py").write_text(
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "role = 'mailer' if '--queues' in sys.argv else sys.argv[1]\n"
        "stop = Path(sys.argv[sys.argv.index('--stop-file') + 1])\n"
        "Path('ready-' + role).write_text(str(os.getpid()))\n"
        f"while (role == 'worker' and {not acknowledge!r}) or not stop.exists():\n"
        "    time.sleep(0.01)\n"
        "Path('finished-' + role).write_text('completed')\n",
        encoding="utf-8",
    )
    discover = launcher.discover
    stop_cooperatively = launcher.stop_cooperatively
    monkeypatch.setattr(
        launcher,
        "discover",
        lambda root, role: (
            psutil.Process(frontend.pid) if role == "frontend" else discover(root, role)
        ),
    )

    def stop_process(root: Path, role: str, process: psutil.Process) -> bool:
        if role == "dispatcher" and cleanup_type is not None:
            raise cleanup_type()
        return stop_cooperatively(
            root, role, process, timeout=0 if role == "worker" and not acknowledge else 5
        )

    monkeypatch.setattr(launcher, "stop_cooperatively", stop_process)
    force_stop = MagicMock(wraps=launcher.terminate_tree)
    monkeypatch.setattr(launcher, "terminate_tree", force_stop)
    monkeypatch.setenv("PYTHONPATH", str(root))
    monkeypatch.setattr(launcher, "PORTS", {})
    monkeypatch.setattr(launcher, "ensure_dependencies", lambda state: None)
    monkeypatch.setattr(launcher, "run_setup", lambda root, args: None)
    monkeypatch.setattr(launcher, "frontend_command", lambda *args, **kwargs: [])

    startup_failure = failure_type("dispatcher startup failed")

    def ready(process: psutil.Process, role: str) -> None:
        if role in ("worker", "mailer", "dispatcher"):
            command = "dispatch" if role == "dispatcher" else role
            wait_for_marker(root / f"ready-{command}")
            assert not (root / ".dev" / f"{role}.stop").exists()
            if role == "dispatcher":
                raise startup_failure

    monkeypatch.setattr(launcher, "wait_ready", ready)
    state: dict[str, Any] = {"root": str(root), "processes": {}}
    previous_children = set(launcher.CHILDREN)
    try:
        with pytest.raises(failure_type) as caught:
            launcher.start(root, state)

        assert caught.value is startup_failure
        force_stop.assert_not_called()
        if cleanup_type is None:
            assert (root / "finished-dispatch").read_text(encoding="utf-8") == "completed"
        else:
            assert launcher.discover(root, "dispatcher") is not None
            assert "dispatcher" in launcher.read_state(root)["processes"]
            assert any("dispatcher" in note for note in caught.value.__notes__)
        assert api.poll() is None
        assert frontend.poll() is None
        worker = launcher.discover(root, "worker")
        assert (worker is None) == acknowledge
        assert ("worker" in launcher.read_state(root)["processes"]) != acknowledge
        if acknowledge:
            assert (root / "finished-worker").read_text(encoding="utf-8") == "completed"
        else:
            assert worker is not None
            assert launcher.recorded_process(root, "worker", state["processes"]["worker"])
            assert any("worker" in note for note in caught.value.__notes__)
    finally:
        for child in set(launcher.CHILDREN) - previous_children:
            if child.poll() is None:
                launcher.terminate_tree(psutil.Process(child.pid))
            child.wait(timeout=5)
            launcher.CHILDREN.remove(child)
