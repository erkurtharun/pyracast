import logging

import pytest

from pyracast.accounts.security import normalize_client_ip
from pyracast.background.failures import log_failure

logger = logging.getLogger("pyracast.tests")


def wrapped_failure() -> RuntimeError:
    try:
        try:
            normalize_client_ip("secret-address")
        except ValueError as error:
            raise RuntimeError("secret wrapper") from error
    except RuntimeError as error:
        return error
    raise AssertionError("normalize_client_ip accepted an invalid address.")


def test_log_failure_names_innermost_pyracast_frame_without_message(
    pyracast_logs: pytest.LogCaptureFixture,
) -> None:
    log_failure(logger, "metering_job_failed", wrapped_failure(), job=7, attempt=2)
    [record] = pyracast_logs.records
    assert record.levelno == logging.ERROR
    assert record.exc_info is None
    line = normalize_client_ip.__code__.co_firstlineno + 1
    assert record.getMessage() == (
        "metering_job_failed job=7 attempt=2 error_type=RuntimeError "
        f"origin=pyracast.accounts.security:{line}:normalize_client_ip"
    )
    assert "secret" not in pyracast_logs.text


def test_log_failure_without_pyracast_frame_names_no_origin(
    pyracast_logs: pytest.LogCaptureFixture,
) -> None:
    log_failure(logger, "calibration_attempt_failed", KeyError("secret"))
    assert [record.getMessage() for record in pyracast_logs.records] == [
        "calibration_attempt_failed error_type=KeyError origin=unknown"
    ]


def test_log_failure_follows_the_implicit_exception_context(
    pyracast_logs: pytest.LogCaptureFixture,
) -> None:
    implicit = KeyError("secret")
    try:
        normalize_client_ip("secret-address")
    except ValueError as error:
        implicit.__context__ = error
    log_failure(logger, "implicit_context", implicit)
    line = normalize_client_ip.__code__.co_firstlineno + 1
    assert [record.getMessage() for record in pyracast_logs.records] == [
        "implicit_context error_type=KeyError "
        f"origin=pyracast.accounts.security:{line}:normalize_client_ip"
    ]


def test_log_failure_honours_suppressed_context_and_stops_at_cycles(
    pyracast_logs: pytest.LogCaptureFixture,
) -> None:
    hidden = wrapped_failure()
    try:
        try:
            raise hidden
        except RuntimeError:
            raise KeyError("secret") from None
    except KeyError as error:
        suppressed = error
    first, second = LookupError("secret"), LookupError("secret")
    first.__context__, second.__context__ = second, first
    log_failure(logger, "suppressed_context", suppressed)
    log_failure(logger, "cyclic_context", first)
    assert [record.getMessage() for record in pyracast_logs.records] == [
        "suppressed_context error_type=KeyError origin=unknown",
        "cyclic_context error_type=LookupError origin=unknown",
    ]
