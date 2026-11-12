import signal
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pyracast import shutdown


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_shutdown_signal_interrupts_wait_and_restores_handlers(
    signum: signal.Signals, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = signal.getsignal(signum)
    with shutdown.shutdown_control() as request:
        handler = signal.getsignal(signum)
        assert callable(handler)
        monkeypatch.setattr(time, "sleep", lambda _: handler(signum, None))

        assert request.wait()
        assert request.requested

    assert signal.getsignal(signum) == previous


def test_shutdown_wait_expires_without_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "monotonic", MagicMock(side_effect=[10.0, 10.0, 10.5]))
    monkeypatch.setattr(time, "sleep", lambda _: None)

    request = shutdown.ShutdownRequest()

    assert not request.wait(0.5)
    assert not request.requested


def test_stop_file_interrupts_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stop_file = tmp_path / "worker.stop"
    with shutdown.shutdown_control(stop_file) as request:
        assert not request.requested
        monkeypatch.setattr(time, "sleep", lambda _: stop_file.touch())

        assert request.wait()
        assert request.requested


def test_shutdown_does_not_replace_handler_installed_by_another_owner() -> None:
    previous = signal.getsignal(signal.SIGTERM)
    try:
        with shutdown.shutdown_control():
            signal.signal(signal.SIGTERM, signal.SIG_IGN)

        assert signal.getsignal(signal.SIGTERM) == signal.SIG_IGN
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_shutdown_restores_handlers_when_registration_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = signal.getsignal(signal.SIGINT)
    install = signal.signal
    calls = 0

    def reject_second_handler(*args: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("handler registration failed")
        return install(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(signal, "signal", reject_second_handler)

    with (
        pytest.raises(ValueError, match="handler registration failed"),
        shutdown.shutdown_control(),
    ):
        pytest.fail("Registration must finish before entering the context")

    assert signal.getsignal(signal.SIGINT) == previous


def test_shutdown_resets_unrestorable_native_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    previous = signal.getsignal(signal.SIGTERM)
    install = signal.signal

    def report_native_handler(*args: object) -> object:
        replaced = install(*args)  # type: ignore[arg-type]
        return None if args[0] == signal.SIGTERM else replaced

    monkeypatch.setattr(signal, "signal", report_native_handler)
    try:
        with shutdown.shutdown_control() as request:
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            handler(signal.SIGTERM, None)
            assert request.requested

        assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL
    finally:
        install(signal.SIGTERM, previous)
