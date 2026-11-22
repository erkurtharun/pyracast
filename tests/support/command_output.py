import io
import logging
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from typing import Any, cast

from django.conf import settings


@contextmanager
def process_streams() -> Iterator[tuple[io.StringIO, io.StringIO]]:
    """stdout and stderr as a command's process has them: what the command prints and every record
    of a handler the logging settings point at either stream."""
    stdout, stderr = io.StringIO(), io.StringIO()
    targets = {"ext://sys.stdout": stdout, "ext://sys.stderr": stderr}
    configured = {
        name: targets[handler["stream"]]
        for name, handler in cast("dict[str, dict[str, str]]", settings.LOGGING["handlers"]).items()
        if handler.get("stream") in targets
    }
    loggers = [
        logging.getLogger(),
        *(
            logger
            for logger in logging.Logger.manager.loggerDict.values()
            if isinstance(logger, logging.Logger)
        ),
    ]
    redirected: dict[logging.StreamHandler[Any], io.StringIO] = {}
    for logger in loggers:
        for handler in logger.handlers:
            target = configured.get(handler.get_name() or "")
            if isinstance(handler, logging.StreamHandler) and target is not None:
                redirected[handler] = target
    assert {handler.get_name() for handler in redirected} == set(configured)
    previous = {handler: handler.stream for handler in redirected}
    for handler, target in redirected.items():
        handler.setStream(target)
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            yield stdout, stderr
    finally:
        for handler, stream in previous.items():
            handler.setStream(stream)
