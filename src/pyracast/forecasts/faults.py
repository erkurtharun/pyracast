import logging
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import date
from math import isfinite
from typing import Literal

from pyracast.errors import DomainError, ErrorCode

logger = logging.getLogger(__name__)

type Scope = Literal["plant", "region"]
CALCULATION_FAULTS: tuple[type[Exception], ...] = (ValueError, ArithmeticError)


class DamagedStoredDataError(ValueError):
    """A stored record whose payload lacks the shape its reader decodes. It names the record
    and the class of the decoding failure, never the stored content."""

    def __init__(self, kind: str, identity: int, fault: str) -> None:
        super().__init__(kind, identity, fault)
        self.kind = kind
        self.identity = identity
        self.fault = fault


class CalculationOutOfRangeError(ValueError):
    """Valid inputs whose figure leaves the range a float represents."""


@contextmanager
def decoding(kind: str, identity: int) -> Iterator[None]:
    try:
        yield
    except DamagedStoredDataError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise DamagedStoredDataError(kind, identity, type(error).__name__) from error


def snapshot_row(row: object, snapshot_id: int) -> AbstractContextManager[None]:
    """The decoding of one interval row; a composed row names the snapshot it came from."""
    source = row.get("source_snapshot_id") if isinstance(row, dict) else None
    return decoding("forecast_snapshot", source if isinstance(source, int) else snapshot_id)


def finite_numbers(value: object) -> None:
    """Refuses a float the JSON decoder made infinite from a stored decimal beyond a float."""
    if isinstance(value, float) and not isfinite(value):
        raise ValueError("A stored number is not finite.")
    items = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
    for item in items:
        finite_numbers(item)


def log_fault(scope: Scope, identifier: int, day: date, step: str, error: Exception) -> None:
    damaged = error if isinstance(error, DamagedStoredDataError) else None
    # The class only: a message of a stored-data fault can quote the stored value.
    logger.warning(
        "forecast_read_fault %s %s day=%s step=%s record=%s error=%s",
        scope,
        identifier,
        day.isoformat(),
        step,
        f"{damaged.kind}:{damaged.identity}" if damaged else None,
        damaged.fault if damaged else type(error).__name__,
    )


@contextmanager
def stored_data(scope: Scope, identifier: int, day: date, step: str) -> Iterator[None]:
    try:
        yield
    except (DamagedStoredDataError, CalculationOutOfRangeError) as error:
        log_fault(scope, identifier, day, step, error)
        raise DomainError(ErrorCode.CALCULATION_OUT_OF_RANGE) from error
