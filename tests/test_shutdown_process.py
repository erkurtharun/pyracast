import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from textwrap import dedent

import psutil
import pytest

from scripts.dev import terminate_tree


def wait_for_text(
    path: Path, expected: str, process: subprocess.Popen[str], *, timeout: float
) -> None:
    started = time.monotonic()
    deadline = started + timeout
    while time.monotonic() < deadline:
        if path.exists() and expected in path.read_text(encoding="utf-8"):
            return
        if process.poll() is not None:
            output, _ = process.communicate()
            raise AssertionError(
                f"Shutdown probe exited with {process.returncode} before {expected!r}: {output}"
            )
        time.sleep(0.01)
    events = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    raise AssertionError(
        f"Shutdown probe did not reach {expected!r} after {time.monotonic() - started:.1f}s "
        f"(pid={process.pid}, returncode={process.poll()}, events={events})"
    )


@pytest.mark.parametrize("method", ["signal", "stop_file"])
@pytest.mark.parametrize("role", ["worker", "dispatch"])
def test_shutdown_finishes_active_work_and_closes_broker(
    tmp_path: Path, role: str, method: str
) -> None:
    if method == "signal" and os.name == "nt":
        pytest.skip("External SIGTERM delivery requires a POSIX process")
    probe = tmp_path / "shutdown_probe.py"
    probe.write_text(
        dedent(
            """
            import sys
            import time
            from pathlib import Path
            from types import SimpleNamespace
            from unittest.mock import MagicMock

            import django
            import dramatiq
            from dramatiq.brokers.stub import StubBroker

            django.setup()
            from pyracast.orchestration import dispatcher, kinds
            from pyracast.orchestration.management.commands import dispatch, worker

            root = Path(sys.argv[1])
            role = sys.argv[2]
            command = worker if role == "worker" else dispatcher

            def record(event):
                with (root / "events").open("a", encoding="utf-8") as output:
                    output.write(event + "\\n")

            def stub_actors(**made):
                kinds.KINDS = tuple(
                    SimpleNamespace(
                        actor=kind.actor,
                        queue=kind.queue,
                        make_actor=made.get(kind.actor, MagicMock()),
                    )
                    for kind in kinds.KINDS
                )

            broker = StubBroker()
            broker.close = lambda: record("closed")
            command.make_broker = lambda: broker
            if role == "dispatch":
                command.record_dispatch_phase = MagicMock()

            if role == "worker":
                @dramatiq.actor(broker=broker, max_retries=0)
                def active_job():
                    record("ready")
                    deadline = time.monotonic() + 120
                    while not (root / "release").exists():
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Parent did not release the active job")
                        time.sleep(0.01)
                    record("finished")

                stub_actors(collect_forecast=lambda _: active_job)
                active_job.send()

                class ObservedWorker(dramatiq.Worker):
                    def stop(self, *args, **kwargs):
                        record("stopping")
                        super().stop(*args, **kwargs)
                        record("stopped")

                command.Worker = ObservedWorker
                command.Command().handle(threads=1, queues=["default"], stop_file=root / "stop")
            else:
                stub_actors()
                command.purge_expired = lambda now: record("ready")
                command.sweep_accounts = MagicMock()
                command.dispatch_collections = MagicMock()
                command.dispatch_calibrations = MagicMock()
                command.dispatch_notifications = MagicMock()
                command.dispatch_epias = MagicMock()
                command.purge_finished_mail = MagicMock()
                command.publish_outbound_mail = MagicMock()
                dispatch.Command().handle(loop=True, poll_seconds=60, stop_file=root / "stop")
            """
        ),
        encoding="utf-8",
    )
    process = subprocess.Popen(  # noqa: S603 -- isolated command probe with a stub broker.
        [sys.executable, str(probe), str(tmp_path), role],
        env={**os.environ, "DJANGO_SETTINGS_MODULE": "pyracast.settings"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        events = tmp_path / "events"
        wait_for_text(events, "ready", process, timeout=60)
        if method == "signal":
            process.send_signal(signal.SIGTERM)
        else:
            (tmp_path / "stop").touch()
        if role == "worker":
            wait_for_text(events, "stopping", process, timeout=30)
            assert process.poll() is None
            (tmp_path / "release").touch()
        output, _ = process.communicate(timeout=30)

        assert process.returncode == 0, output
        assert events.read_text(encoding="utf-8").splitlines() == (
            ["ready", "stopping", "finished", "stopped", "closed"]
            if role == "worker"
            else ["ready", "closed"]
        )
    finally:
        if process.poll() is None:
            terminate_tree(psutil.Process(process.pid))
        process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
