import logging
import traceback
from pathlib import Path

_PACKAGE = Path(__file__).resolve().parents[1]


def _chain(error: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and all(current is not seen for seen in chain):
        chain.append(current)
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return chain


def _origin(error: BaseException) -> str:
    for exception in reversed(_chain(error)):
        for frame in reversed(traceback.extract_tb(exception.__traceback__)):
            path = Path(frame.filename).resolve()
            if path.is_relative_to(_PACKAGE):
                module = ".".join(path.relative_to(_PACKAGE.parent).with_suffix("").parts)
                return f"{module}:{frame.lineno}:{frame.name}"
    return "unknown"


def log_failure(logger: logging.Logger, event: str, error: BaseException, **fields: object) -> None:
    """Log a background failure by type and code location.

    Never logs str(error) or the traceback: provider errors and URLs can carry secrets.
    """
    names = "".join(f" {name}=%s" for name in fields)
    logger.error(
        f"{event}{names} error_type=%s origin=%s",
        *fields.values(),
        type(error).__name__,
        _origin(error),
    )
