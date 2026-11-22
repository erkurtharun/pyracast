import copy
import io
import logging
import logging.config
import re
import sys
import threading
from collections.abc import Buffer, Callable
from pathlib import Path
from runpy import run_path
from typing import Any

import pytest
from django.conf import settings

import pyracast.settings as project_settings
from pyracast.log_lines import CONTINUATION, gunicorn_logging, one_record

# The deployed bound: a pipe keeps a write of at most PIPE_BUF bytes whole (4096 on Linux).
PIPE_BUF = 4096
# 2, 3, 4 and 1 bytes a character in UTF-8.
FILLER = "ğ€😀a"


class RecordingRaw(io.RawIOBase):
    """A file descriptor that records every write call made to it."""

    name = "recording"

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[bytes] = []

    def writable(self) -> bool:
        return True

    def write(self, data: Buffer, /) -> int:
        written = bytes(data)
        self.calls.append(written)
        return len(written)


# The text layers Python puts over a process's stdout and stderr, and the line break each writes.
STREAMS: dict[str, tuple[Callable[[RecordingRaw], io.TextIOWrapper], str]] = {
    "buffered": (
        lambda raw: io.TextIOWrapper(io.BufferedWriter(raw), encoding="utf-8", newline="\n"),
        "\n",
    ),
    "line buffered": (
        lambda raw: io.TextIOWrapper(
            io.BufferedWriter(raw), encoding="utf-8", newline="\n", line_buffering=True
        ),
        "\n",
    ),
    "unbuffered": (
        lambda raw: io.TextIOWrapper(raw, encoding="utf-8", newline="\n", write_through=True),
        "\n",
    ),
    "translating line breaks": (
        lambda raw: io.TextIOWrapper(io.BufferedWriter(raw), encoding="utf-8", newline="\r\n"),
        "\r\n",
    ),
}


def configured_handler(configuration: dict[str, Any], name: str) -> logging.Handler:
    """A handler of a logging configuration as dictConfig builds it."""
    configurator = logging.config.DictConfigurator(copy.deepcopy(configuration))
    formatters = configurator.config["formatters"]
    for formatter in list(formatters):
        formatters[formatter] = configurator.configure_formatter(formatters[formatter])
    handler: logging.Handler = configurator.configure_handler(configurator.config["handlers"][name])
    return handler


def deployed_handler(name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> logging.Handler:
    if name == "security to a path":
        monkeypatch.setenv("PYRACAST_SECURITY_LOG_PATH", str(tmp_path / "container-stream"))
        routed = run_path(str(Path(project_settings.__file__)))["LOGGING"]
        return configured_handler(routed, "security")
    if name.startswith("gunicorn "):
        return configured_handler(gunicorn_logging(), name.removeprefix("gunicorn "))
    return configured_handler(settings.LOGGING, name)


def test_every_line_after_the_first_of_a_record_is_indented() -> None:
    assert one_record("start\r\nsecond\rthird\n") == (
        f"start\n{CONTINUATION}second\n{CONTINUATION}third\n{CONTINUATION}"
    )


@pytest.mark.parametrize("shift", range(len(FILLER.encode())))
def test_a_long_line_continues_on_indented_lines_cut_between_characters(shift: int) -> None:
    text = "x" * shift + FILLER * 3000 + "\n" + FILLER * 3000

    lines = one_record(text).split("\n")

    assert text.startswith(lines[0])
    assert all(line.startswith(CONTINUATION) for line in lines[1:])
    assert max(len(line.encode()) + len("\r\n") for line in lines) <= PIPE_BUF
    assert lines[0] + "".join(line.removeprefix(CONTINUATION) for line in lines[1:]) == (
        text.replace("\n", "")
    )


@pytest.mark.parametrize("stream", STREAMS)
@pytest.mark.parametrize(
    "handler",
    ["console", "security", "security to a path", "gunicorn stdout", "gunicorn stderr"],
)
def test_every_line_of_a_record_is_its_own_write_call_of_at_most_4096_bytes(
    handler: str, stream: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = RecordingRaw()
    build, line_break = STREAMS[stream]
    target = build(raw)
    deployed = deployed_handler(handler, tmp_path, monkeypatch)
    assert isinstance(deployed, logging.StreamHandler)
    deployed.setStream(target)
    target.write("unflushed text of another writer")
    record = logging.makeLogRecord(
        {
            "name": "pyracast.forecasts",
            "levelno": logging.WARNING,
            "levelname": "WARNING",
            "msg": "%s\n%s",
            "args": (FILLER * 3000, "ş" * 5000),
        }
    )

    deployed.handle(record)
    deployed.close()

    lines = deployed.format(record).split("\n")
    assert len(lines) > 4
    assert raw.calls == [
        b"unflushed text of another writer",
        *((line + line_break).encode() for line in lines),
    ]
    assert max(len(call) for call in raw.calls) <= PIPE_BUF


def warning(message: str) -> logging.LogRecord:
    return logging.makeLogRecord(
        {
            "name": "pyracast.forecasts",
            "levelno": logging.WARNING,
            "levelname": "WARNING",
            "msg": message,
        }
    )


def test_the_security_handler_opens_its_path_with_the_first_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployed = deployed_handler("security to a path", tmp_path, monkeypatch)
    record = warning("first\nsecond")

    deployed.handle(record)
    deployed.close()

    assert (tmp_path / "container-stream").read_text(encoding="utf-8") == (
        deployed.format(record) + "\n"
    )


class FailingStream(io.StringIO):
    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self.error = error

    def write(self, text: str, /) -> int:
        raise self.error


def test_a_line_that_cannot_be_written_is_reported_to_the_handler_not_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployed = configured_handler(settings.LOGGING, "console")
    assert isinstance(deployed, logging.StreamHandler)
    reported: list[logging.LogRecord] = []
    monkeypatch.setattr(deployed, "handleError", reported.append)
    deployed.setStream(FailingStream(OSError("stream closed")))
    record = warning("unwritten")

    deployed.handle(record)

    assert reported == [record]


class Unreadable:
    def __init__(self, text: str) -> None:
        self.text = text

    def __str__(self) -> str:
        raise ValueError(self.text)


DEPLOYED = ["console", "security", "security to a path", "gunicorn stdout", "gunicorn stderr"]
UNFORMATTABLE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2},[0-9]{3} WARNING pyracast[.]forecasts "
    rf"unformattable_record error_type=ValueError origin={re.escape(__file__)}:7\n"
)


@pytest.mark.parametrize("handler", DEPLOYED)
def test_a_record_that_cannot_be_formatted_is_one_line_naming_its_logger_and_error(
    handler: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = RecordingRaw()
    target = STREAMS["buffered"][0](raw)
    deployed = deployed_handler(handler, tmp_path, monkeypatch)
    assert isinstance(deployed, logging.StreamHandler)
    deployed.setStream(target)
    errors = io.StringIO()
    monkeypatch.setattr(sys, "stderr", errors)
    line = "2026-11-22 00:00:00,000 INFO pyracast.security {}"
    record = logging.getLogger("pyracast.forecasts").makeRecord(
        "pyracast.forecasts", logging.WARNING, __file__, 7, Unreadable(f"x\n{line}"), (), None
    )

    deployed.handle(record)
    deployed.close()

    (call,) = raw.calls
    assert UNFORMATTABLE.fullmatch(call.decode())
    assert errors.getvalue() == ""


class SteppedStream(io.TextIOWrapper):
    """A buffered stream that holds the first thread at its first flush until the second thread
    has written a line or waits for the lock, and the second thread after its first write until
    the first thread has written."""

    def __init__(self, raw: RecordingRaw, second: Callable[[], object]) -> None:
        super().__init__(io.BufferedWriter(raw), encoding="utf-8", newline="\n")
        self.second = second
        self.first_thread = threading.get_ident()
        self.started: threading.Thread | None = None
        self.settled = threading.Event()
        self.first_wrote = threading.Event()

    def flush(self) -> None:
        super().flush()
        if threading.get_ident() == self.first_thread and self.started is None:
            self.started = threading.Thread(target=self.second)
            self.started.start()
            assert self.settled.wait(timeout=10)

    def write(self, text: str, /) -> int:
        written = super().write(text)
        if threading.get_ident() == self.first_thread:
            self.first_wrote.set()
        elif not self.settled.is_set():
            self.settled.set()
            assert self.first_wrote.wait(timeout=10)
        return written


class ProbedLock:
    """A handler's lock that tells the stream when its thread would wait for it."""

    def __init__(self, lock: Any, stream: SteppedStream) -> None:
        self.lock = lock
        self.stream = stream

    def __enter__(self) -> None:
        if not self.lock.acquire(blocking=False):
            self.stream.settled.set()
            self.lock.acquire()

    def __exit__(self, *exception: object) -> None:
        self.lock.release()


@pytest.mark.parametrize(
    "handlers", [("console", "gunicorn stderr"), ("gunicorn stdout", "security")]
)
def test_handlers_sharing_a_buffered_stream_write_each_line_in_a_call_of_its_own(
    handlers: tuple[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = (deployed_handler(name, tmp_path, monkeypatch) for name in handlers)
    assert isinstance(first, logging.StreamHandler)
    assert isinstance(second, logging.StreamHandler)
    raw = RecordingRaw()
    records = [warning(f"{name} {'x' * 4000}") for name in handlers]
    target = SteppedStream(raw, lambda: second.handle(records[1]))
    first.setStream(target)
    second.setStream(target)
    monkeypatch.setattr(second, "lock", ProbedLock(second.lock, target))

    first.handle(records[0])
    assert target.started is not None
    target.started.join()

    assert raw.calls == [
        (handler.format(record) + "\n").encode()
        for handler, record in zip((first, second), records, strict=True)
    ]
    assert max(len(call) for call in raw.calls) <= PIPE_BUF


def test_a_recursion_error_while_writing_reaches_the_caller() -> None:
    deployed = configured_handler(settings.LOGGING, "console")
    assert isinstance(deployed, logging.StreamHandler)
    deployed.setStream(FailingStream(RecursionError()))

    with pytest.raises(RecursionError):
        deployed.handle(warning("unwritten"))


class Recursing:
    def __str__(self) -> str:
        raise RecursionError


def test_a_recursion_error_while_formatting_reaches_the_caller() -> None:
    deployed = configured_handler(settings.LOGGING, "console")
    assert isinstance(deployed, logging.StreamHandler)
    deployed.setStream(io.StringIO())
    record = warning("unformatted")
    record.msg = Recursing()

    with pytest.raises(RecursionError):
        deployed.handle(record)
