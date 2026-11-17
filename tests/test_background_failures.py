import logging

import pytest
from pyracast.background.failures import log_failure

from pyracast.accounts.security import normalize_client_ip

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
