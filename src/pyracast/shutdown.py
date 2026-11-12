import signal
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from math import inf
from pathlib import Path
from types import FrameType


@dataclass
class ShutdownRequest:
    stop_file: Path | None = None
    _requested: bool = False

    @property
    def requested(self) -> bool:
        return self._requested or (self.stop_file is not None and self.stop_file.exists())

    def wait(self, timeout: float | None = None) -> bool:
        deadline = inf if timeout is None else time.monotonic() + timeout
        while not self.requested:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.1, remaining))
        return True


@contextmanager
def shutdown_control(stop_file: Path | None = None) -> Iterator[ShutdownRequest]:
    shutdown = ShutdownRequest(stop_file=stop_file)

    def request_shutdown(signum: int, frame: FrameType | None) -> None:
        # Signal handlers cannot safely acquire an Event's internal lock.
        shutdown._requested = True

    previous = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, request_shutdown)
        yield shutdown
    finally:
        for signum, handler in previous.items():
            if signal.getsignal(signum) is request_shutdown:
                signal.signal(signum, signal.SIG_DFL if handler is None else handler)
