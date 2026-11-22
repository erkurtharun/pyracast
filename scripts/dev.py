import argparse
import errno
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

import httpx
import psutil

from pyracast.background.deadlines import DRAIN, STOP_GRACE
from pyracast.environment import load_environment
from scripts.dev_dependencies import ensure_dependencies

ROOT = Path(__file__).resolve().parents[1]
ROLES = ("api", "worker", "mailer", "dispatcher", "frontend")
BACKGROUND_ROLES = ("worker", "mailer", "dispatcher")
PORTS = {"api": 8000, "frontend": 3000}

if sys.platform == "win32":
    import msvcrt

    def _lock_file(stream: BinaryIO) -> None:
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock_file(stream: BinaryIO) -> None:
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock_file(stream: BinaryIO) -> None:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_file(stream: BinaryIO) -> None:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def command_lock(directory: Path, *, timeout: float = 0) -> Iterator[None]:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "launcher.lock").open("a+b") as stream:
        stream.seek(0)
        deadline = time.monotonic() + timeout
        waiting = False
        while True:
            try:
                _lock_file(stream)
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(
                        "Timed out waiting for another launcher command. "
                        "Let it finish, then retry; setup progress is in .dev/setup.log."
                    ) from None
                if not waiting:
                    print(
                        "Another launcher command is running; waiting for it to finish...",
                        flush=True,
                    )
                    waiting = True
                time.sleep(min(0.25, remaining))
        if waiting:
            print("Previous launcher command finished; continuing.", flush=True)
        try:
            yield
        finally:
            _unlock_file(stream)


def read_state(root: Path) -> dict[str, Any]:
    path = root / ".dev/state.json"
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if (
        not isinstance(state, dict)
        or state.get("root", str(root)) != str(root)
        or not isinstance(state.get("processes", {}), dict)
    ):
        raise RuntimeError("Launcher state belongs to another workspace or is invalid.")
    state.setdefault("root", str(root))
    state.setdefault("processes", {})
    return state


def save_state(root: Path, state: dict[str, Any]) -> None:
    path = root / ".dev/state.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temporary.replace(path)


def process_role(process: psutil.Process, root: Path) -> str | None:
    try:
        cwd = Path(process.cwd()).resolve()
        args = process.cmdline()
        backend = args[1:3] == ["-m", "django"] or any(
            Path(arg).name.lower() in ("pyracast", "pyracast.exe") for arg in args
        )
        if cwd == root and backend:
            for command_name, role in (
                ("runserver", "api"),
                ("worker", "worker"),
                ("dispatch", "dispatcher"),
            ):
                if command_name in args:
                    if command_name == "worker" and "--queues" in args:
                        queue_index = args.index("--queues") + 1
                        if args[queue_index : queue_index + 1] == ["notifications"]:
                            return "mailer"
                    return role
        if (
            cwd in (root, root / "web")
            and "dev" in args
            and len(args) > 1
            and Path(args[1]).resolve() == root / "web/node_modules/next/dist/bin/next"
        ):
            return "frontend"
    except psutil.Error, OSError:
        pass
    return None


def identity(process: psutil.Process) -> dict[str, Any]:
    return {
        "pid": process.pid,
        "created": process.create_time(),
        "exe": process.exe(),
        "command_sha256": hashlib.sha256(json.dumps(process.cmdline()).encode()).hexdigest(),
    }


def recorded_process(root: Path, role: str, record: dict[str, Any]) -> psutil.Process | None:
    try:
        process = psutil.Process(record["pid"])
        if identity(process) == record and process_role(process, root) == role:
            return process
    except psutil.Error, KeyError, TypeError:
        pass
    return None


def discover(root: Path, role: str) -> psutil.Process | None:
    matches = [p for p in psutil.process_iter() if process_role(p, root) == role]
    ids = {p.pid for p in matches}
    parents = [p for p in matches if p.ppid() not in ids]
    if len(parents) > 1:
        raise RuntimeError(f"Multiple {role} processes exist; stop the duplicates first.")
    return parents[0] if parents else None


def listener_pid(port: int) -> int | None:
    for connection in psutil.net_connections(kind="tcp"):
        if (
            connection.laddr
            and connection.status == psutil.CONN_LISTEN
            and connection.laddr.port == port
        ):
            return connection.pid
    return None


def owns_port(process: psutil.Process, port: int) -> bool:
    return listener_pid(port) in {process.pid, *(p.pid for p in process.children(recursive=True))}


def terminate_tree(process: psutil.Process) -> None:
    try:
        children = process.children(recursive=True)
        targets = [process, *reversed(children)]
        for target in targets:
            if target.is_running():
                target.terminate()
        _, alive = psutil.wait_procs(targets, timeout=10)
        for target in alive:
            target.kill()
        psutil.wait_procs(alive, timeout=5)
    except psutil.NoSuchProcess:
        pass


def stop_file_path(root: Path, role: str) -> Path:
    stop_file = (root / ".dev" / f"{role}.stop").resolve()
    if not stop_file.is_relative_to(root.resolve()):
        raise RuntimeError("Shutdown marker must remain inside the workspace.")
    return stop_file


def process_stop_file(root: Path, role: str, process: psutil.Process) -> Path | None:
    if role not in BACKGROUND_ROLES:
        return None
    stop_file = stop_file_path(root, role)
    args = process.cmdline()
    return (
        stop_file
        if any(
            args[index : index + 2] == ["--stop-file", str(stop_file)] for index in range(len(args))
        )
        else None
    )


def stop_cooperatively(
    root: Path, role: str, process: psutil.Process, timeout: float = STOP_GRACE.total_seconds()
) -> bool:
    try:
        stop_file = process_stop_file(root, role, process)
        if stop_file is None:
            return False
        stop_file.touch()
        process.wait(timeout=timeout)
    except psutil.NoSuchProcess:
        pass
    except psutil.TimeoutExpired:
        raise RuntimeError(
            f"{role} is still shutting down and was left running. Retry stop later."
        ) from None
    return True


def run_setup(root: Path, args: list[str]) -> None:
    try:
        with (root / ".dev/setup.log").open("ab") as output:
            result = subprocess.run(  # noqa: S603 -- fixed toolchain commands.
                args, cwd=root, stdout=output, stderr=output, check=False, timeout=300
            )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Setup timed out; see .dev/setup.log.") from None
    if result.returncode:
        raise RuntimeError("Setup failed; see .dev/setup.log.")


def npm_cli(npm: str, node: str) -> Path:
    resolved = Path(npm).resolve()
    node_directory = Path(node).resolve().parent
    package = Path("node_modules/npm/bin/npm-cli.js")
    candidates = [
        *([resolved] if resolved.suffix == ".js" else []),
        resolved.parent / package,
        node_directory / package,
        node_directory.parent / "lib" / package,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    tried = ", ".join(str(candidate) for candidate in candidates)
    raise RuntimeError(f"Cannot locate npm-cli.js for the installed Node.js; tried {tried}.")


def frontend_command(root: Path, *, running: bool = False) -> list[str]:
    node = shutil.which("node")
    npm = shutil.which("npm")
    if node is None or npm is None:
        raise RuntimeError("Install Node.js 26.13.0 LTS (including npm) first.")
    version = subprocess.check_output([node, "--version"], text=True).strip()  # noqa: S603
    if tuple(int(part) for part in version.lstrip("v").split(".")) < (26, 13, 0):
        raise RuntimeError("Node.js 26.13.0 or later is required.")
    lock_hash = hashlib.sha256((root / "web/package-lock.json").read_bytes()).hexdigest()
    marker = root / ".dev/npm-lock.sha256"
    installed = marker.read_text() if marker.exists() else ""
    if installed != lock_hash or not (root / "web/node_modules/next").is_dir():
        if running:
            raise RuntimeError("Frontend dependencies changed. Run stop.cmd, then start.cmd.")
        cli = npm_cli(npm, node)
        print(
            "Installing locked frontend dependencies (first start or lockfile change)...",
            flush=True,
        )
        run_setup(
            root,
            [node, str(cli), "--prefix", "web", "ci", "--ignore-scripts", "--prefer-offline"],
        )
        marker.write_text(lock_hash, encoding="utf-8")
    return [
        node,
        str(root / "web/node_modules/next/dist/bin/next"),
        "dev",
        "--hostname",
        "127.0.0.1",
        "--port",
        "3000",
    ]


def wait_ready(process: psutil.Process, role: str, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            raise RuntimeError(f"{role} exited; see .dev/{role}.log.")
        if role not in PORTS:
            time.sleep(1)
            if process.is_running():
                return
        else:
            port = PORTS[role]
            if owns_port(process, port):
                try:
                    path = "/api/session/" if role == "api" else "/"
                    response = httpx.get(f"http://127.0.0.1:{port}{path}", timeout=3)
                    if response.status_code == 200 and (
                        role != "api" or "csrf_token" in response.json()
                    ):
                        return
                except httpx.HTTPError, ValueError:
                    pass
        time.sleep(0.25)
    raise RuntimeError(f"{role} did not become ready; see .dev/{role}.log.")


def start(root: Path, state: dict[str, Any]) -> None:
    existing = {role: discover(root, role) for role in ROLES}
    for role in BACKGROUND_ROLES:
        process = existing[role]
        if process is not None:
            stop_file = process_stop_file(root, role, process)
            if stop_file is not None and stop_file.exists():
                raise RuntimeError(f"{role} is still shutting down. Retry start after it exits.")
    for role, port in PORTS.items():
        process = existing[role]
        if listener_pid(port) is not None and (process is None or not owns_port(process, port)):
            raise RuntimeError(f"Port {port} belongs to another process; no services were started.")
    print("Checking PostgreSQL and RabbitMQ...", flush=True)
    ensure_dependencies()
    commands = {
        "api": [sys.executable, "-m", "django", "runserver", "127.0.0.1:8000", "--noreload"],
        "frontend": frontend_command(root, running=existing["frontend"] is not None),
        "worker": [sys.executable, "-m", "django", "worker", "--threads", "4"],
        "mailer": [
            sys.executable,
            "-m",
            "django",
            "worker",
            "--threads",
            "2",
            "--queues",
            "notifications",
        ],
        "dispatcher": [sys.executable, "-m", "django", "dispatch", "--loop"],
    }
    print("Compiling translation catalogs...", flush=True)
    run_setup(root, [sys.executable, "scripts/translations.py"])
    print("Applying database migrations...", flush=True)
    run_setup(root, [sys.executable, "-m", "django", "migrate", "--noinput"])
    launched: list[tuple[str, psutil.Process, subprocess.Popen[bytes]]] = []
    try:
        for role, args in commands.items():
            process = existing[role]
            if process is None:
                if role in PORTS and listener_pid(PORTS[role]) is not None:
                    raise RuntimeError(
                        f"Port {PORTS[role]} belongs to another process; {role} was not started."
                    )
                if role in BACKGROUND_ROLES:
                    stop_file = stop_file_path(root, role)
                    stop_file.unlink(missing_ok=True)
                    args = [*args, "--stop-file", str(stop_file)]
                with (root / f".dev/{role}.log").open("ab") as output:
                    child = subprocess.Popen(  # noqa: S603 -- fixed local service commands.
                        args,
                        cwd=root / "web" if role == "frontend" else root,
                        stdout=output,
                        stderr=output,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        start_new_session=os.name != "nt",
                    )
                process = psutil.Process(child.pid)
                launched.append((role, process, child))
            state["processes"][role] = identity(process)
            save_state(root, state)
            wait_ready(process, role)
            actual = discover(root, role)
            if actual is None:
                raise RuntimeError(f"Cannot identify the running {role} process.")
            state["processes"][role] = identity(actual)
            save_state(root, state)
            print(f"{role}: ready", flush=True)
    except BaseException as startup_error:
        for role, process, child in reversed(launched):
            try:
                if not stop_cooperatively(root, role, process):
                    terminate_tree(process)
                child.wait(timeout=15)
            except (RuntimeError, OSError, psutil.Error, subprocess.TimeoutExpired) as error:
                startup_error.add_note(
                    f"{role} cleanup failed ({type(error).__name__}); its process identity was "
                    "retained. Retry stop to finish cleanup."
                )
                continue
            state["processes"].pop(role, None)
        try:
            save_state(root, state)
        except OSError:
            startup_error.add_note("Cleanup state could not be saved; inspect running processes.")
        raise
    print("Open http://127.0.0.1:3000. Services stay running; use stop.cmd to stop them.")


def wait_for_jobs(timeout: float = DRAIN.total_seconds()) -> None:
    import django

    django.setup()
    from pyracast.accounts.models import OutboundMail
    from pyracast.forecasts.models import CollectionJob
    from pyracast.metering.models import EpiasCollectionJob
    from pyracast.notifications.models import NotificationDelivery
    from pyracast.plants.models import CalibrationRun

    deadline = time.monotonic() + timeout
    while any(
        model.objects.filter(status="running").exists()
        for model in (CollectionJob, EpiasCollectionJob, CalibrationRun)
    ) or any(
        model.objects.filter(status="sending").exists()
        for model in (NotificationDelivery, OutboundMail)
    ):
        if time.monotonic() >= deadline:
            raise RuntimeError("Jobs are still running; worker was left running. Retry stop later.")
        time.sleep(1)


def stop(root: Path, state: dict[str, Any], frontend_only: bool = False) -> None:
    for role in (
        ("frontend",) if frontend_only else ("frontend", "dispatcher", "mailer", "worker", "api")
    ):
        record = state["processes"].get(role, {})
        process = recorded_process(root, role, record)
        if process is not None:
            print(
                f"Stopping {role} (up to {round(STOP_GRACE.total_seconds() / 60)} minutes "
                "for background work)...",
                flush=True,
            )
            if not stop_cooperatively(root, role, process):
                if role in ("worker", "mailer"):
                    wait_for_jobs()
                terminate_tree(process)
        state["processes"].pop(role, None)
        save_state(root, state)
        print(f"{role}: stopped" if process else f"{role}: not managed/running")
    print("PostgreSQL and RabbitMQ were left running; database files were not changed.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Start, stop or inspect the local development stack."
    )
    parser.add_argument("action", choices=("start", "stop", "status"))
    parser.add_argument("--frontend-only", action="store_true")
    args = parser.parse_args()
    os.chdir(ROOT)
    load_environment()
    os.environ.update(DJANGO_SETTINGS_MODULE="pyracast.settings", PYRACAST_DEBUG="1")
    origins = os.environ.get("PYRACAST_CSRF_ORIGINS", "").split()
    os.environ["PYRACAST_CSRF_ORIGINS"] = " ".join(
        dict.fromkeys([*origins, "http://127.0.0.1:3000", "http://localhost:3000"])
    )
    try:
        with command_lock(ROOT / ".dev", timeout=600):
            state = read_state(ROOT)
            if args.action == "start":
                start(ROOT, state)
            elif args.action == "stop":
                stop(ROOT, state, args.frontend_only)
            else:
                for role in ROLES:
                    process = recorded_process(ROOT, role, state["processes"].get(role, {}))
                    print(f"{role}: {'running' if process else 'stopped/unmanaged'}")
        return 0
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        for note in getattr(error, "__notes__", ()):
            print(note, file=sys.stderr)
        return 1
    except OSError, ValueError, psutil.Error:
        print(
            "Launcher could not complete. Check .dev logs and service configuration.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
