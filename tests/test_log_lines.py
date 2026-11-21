import copy
import io
import logging
import logging.config
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


def test_a_recursion_error_while_writing_reaches_the_caller() -> None:
    deployed = configured_handler(settings.LOGGING, "console")
    assert isinstance(deployed, logging.StreamHandler)
    deployed.setStream(FailingStream(RecursionError()))

    with pytest.raises(RecursionError):
        deployed.handle(warning("unwritten"))
