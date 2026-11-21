import logging
import logging.config
import re
from collections.abc import Iterator
from typing import Any, TextIO

# Every record of the application and of Gunicorn starts with this; the security log filter in
# infra/terraform/monitoring.tf matches the start of its first line.
LINE_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
# Every physical line of a record but the first starts with it, so only a record's own start
# reaches column 0 whatever its message or traceback carries.
CONTINUATION = "    "
# The processes of a container share its stdout and stderr pipes, and a pipe keeps a write whole
# only up to PIPE_BUF, 4096 bytes on Linux. So every physical line is one write of at most that
# many bytes: UTF-8 text, its continuation and a line break of up to two bytes ("\r\n").
PIPE_BUF = 4096
PIECE_BYTES = PIPE_BUF - len("\r\n") - len(CONTINUATION)
LINE_BREAK = re.compile(r"\r\n|\r|\n")


def physical_lines(text: str) -> Iterator[str]:
    for line in LINE_BREAK.split(text):
        encoded = line.encode(errors="surrogatepass")
        while len(encoded) > PIECE_BYTES:
            end = PIECE_BYTES
            while encoded[end] & 0xC0 == 0x80:  # a UTF-8 continuation byte
                end -= 1
            yield encoded[:end].decode(errors="surrogatepass")
            encoded = encoded[end:]
        yield encoded.decode(errors="surrogatepass")


def one_record(text: str) -> str:
    first, *rest = physical_lines(text)
    return "\n".join([first, *(CONTINUATION + line for line in rest)])


class LineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return one_record(super().format(record))


def write_lines(handler: logging.StreamHandler[Any], record: logging.LogRecord) -> None:
    """Each line in a write call of its own, after any text already waiting on the stream."""
    try:
        text = handler.format(record)
        stream = handler.stream
        stream.flush()
        for line in text.split("\n"):
            stream.write(line + handler.terminator)
            stream.flush()
    except RecursionError:
        raise
    except Exception:
        handler.handleError(record)


class LineHandler(logging.StreamHandler[TextIO]):
    def emit(self, record: logging.LogRecord) -> None:
        write_lines(self, record)


class LineFileHandler(logging.FileHandler):
    def emit(self, record: logging.LogRecord) -> None:
        if self.stream is None:
            self.stream = self._open()
        write_lines(self, record)


def configure(settings: dict[str, Any]) -> None:
    """Django's LOGGING_CONFIG: warnings become records, so they keep the same line rule."""
    logging.config.dictConfig(settings)
    logging.captureWarnings(True)


def gunicorn_logging() -> dict[str, Any]:
    """Gunicorn's logconfig_dict: its error and access records in the application's lines."""

    def handler(stream: str) -> dict[str, str]:
        return {"class": f"{__name__}.LineHandler", "formatter": "line", "stream": stream}

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"line": {"class": f"{__name__}.LineFormatter", "format": LINE_FORMAT}},
        "handlers": {"stdout": handler("ext://sys.stdout"), "stderr": handler("ext://sys.stderr")},
        "root": {"level": "WARNING", "handlers": ["stderr"]},
        "loggers": {
            "gunicorn.error": {"level": "INFO", "handlers": ["stderr"], "propagate": False},
            "gunicorn.access": {"level": "INFO", "handlers": ["stdout"], "propagate": False},
        },
    }
