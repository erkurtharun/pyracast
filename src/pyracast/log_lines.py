import logging
import logging.config
import re
from collections.abc import Iterator
from typing import Any

# Every record of the application and of Gunicorn starts with this; the security log filter in
# infra/terraform/monitoring.tf matches the start of its first line.
LINE_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
# Every physical line of a record but the first starts with it, so only a record's own start
# reaches column 0 whatever its message or traceback carries.
CONTINUATION = "    "
# Docker cuts a line longer than 16 KiB into separate log entries; at most 4 bytes a character
# keeps every physical line below that.
LINE_CHARACTERS = 4000
LINE_BREAK = re.compile(r"\r\n|\r|\n")


def physical_lines(text: str) -> Iterator[str]:
    for line in LINE_BREAK.split(text):
        yield line[:LINE_CHARACTERS]
        for start in range(LINE_CHARACTERS, len(line), LINE_CHARACTERS):
            yield line[start : start + LINE_CHARACTERS]


def one_record(text: str) -> str:
    first, *rest = physical_lines(text)
    return "\n".join([first, *(CONTINUATION + line for line in rest)])


class LineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return one_record(super().format(record))


def configure(settings: dict[str, Any]) -> None:
    """Django's LOGGING_CONFIG: warnings become records, so they keep the same line rule."""
    logging.config.dictConfig(settings)
    logging.captureWarnings(True)


def gunicorn_logging() -> dict[str, Any]:
    """Gunicorn's logconfig_dict: its error and access records in the application's lines."""

    def handler(stream: str) -> dict[str, str]:
        return {"class": "logging.StreamHandler", "formatter": "line", "stream": stream}

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
