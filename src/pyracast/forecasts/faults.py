import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from typing import Literal

from pyracast.errors import DomainError, ErrorCode

logger = logging.getLogger(__name__)

type Scope = Literal["plant", "region"]
CALCULATION_FAULTS: tuple[type[Exception], ...] = (ValueError, ArithmeticError)
# A frozen payload saved by an earlier code version can lack a key or hold another type.
FROZEN_PAYLOAD_FAULTS: tuple[type[Exception], ...] = (*CALCULATION_FAULTS, KeyError, TypeError)


def log_fault(scope: Scope, identifier: int, day: date, step: str, error: Exception) -> None:
    # The class only: a message of a stored-data fault can quote the stored value.
    logger.warning(
        "forecast_read_fault %s %s day=%s step=%s error=%s",
        scope,
        identifier,
        day.isoformat(),
        step,
        type(error).__name__,
    )


@contextmanager
def stored_data(
    scope: Scope,
    identifier: int,
    day: date,
    step: str,
    faults: tuple[type[Exception], ...] = CALCULATION_FAULTS,
) -> Iterator[None]:
    try:
        yield
    except faults as error:
        log_fault(scope, identifier, day, step, error)
        raise DomainError(ErrorCode.CALCULATION_OUT_OF_RANGE) from error
