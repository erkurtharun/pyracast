import logging
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import OperationalError, connection, transaction
from psycopg.errors import DeadlockDetected
from rest_framework.response import Response

from pyracast.accounts.models import Tenant
from pyracast.api.errors import exception_handler


@pytest.mark.django_db(transaction=True)
def test_lock_contention_returns_retryable_response_without_exposing_database_details() -> None:
    tenant = Tenant.objects.create(name="Contended workspace")

    def update() -> Response | None:
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '100ms'")
                Tenant.objects.filter(pk=tenant.pk).update(name="Unexpected update")
        except OperationalError as error:
            return exception_handler(error, {})
        finally:
            connection.close()
        raise AssertionError("The locked row was updated before its owner released it.")

    with ThreadPoolExecutor(max_workers=1) as pool, transaction.atomic():
        Tenant.objects.select_for_update().get(pk=tenant.pk)
        response = pool.submit(update).result(timeout=15)

    assert response is not None
    assert response.status_code == 503
    assert response.data == {
        "code": "busy",
        "message": "This could not be saved right now. Try again in a moment.",
        "retry_after": 1,
    }
    assert response["Retry-After"] == "1"
    tenant.refresh_from_db()
    assert tenant.name == "Contended workspace"


def test_deadlock_is_retryable_but_not_reported_as_lock_timeout() -> None:
    error = OperationalError("Database operation failed")
    error.__cause__ = DeadlockDetected("deadlock detected")
    response = exception_handler(error, {})
    assert response is not None
    assert response.status_code == 503
    assert response.data == {
        "code": "database_conflict",
        "message": "This could not be saved right now. Try again in a moment.",
        "retry_after": 1,
    }
    assert response["Retry-After"] == "1"


def test_unclassified_database_error_is_a_logged_server_error(
    pyracast_logs: pytest.LogCaptureFixture,
) -> None:
    response = exception_handler(OperationalError("private database detail"), {})
    assert response is not None
    assert response.status_code == 500
    assert response.data["code"] == "server_error"
    [record] = [record for record in pyracast_logs.records if record.name == "pyracast.api"]
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert f"request_id={response.data['request_id']}" in record.getMessage()
    assert "private database detail" not in str(response.data)


@pytest.mark.django_db(transaction=True)
def test_statement_timeout_answers_503_request_timeout(
    pyracast_logs: pytest.LogCaptureFixture,
) -> None:
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = '50ms'")
            cursor.execute("SELECT pg_sleep(2)")
    except OperationalError as error:
        response = exception_handler(error, {})
    else:
        raise AssertionError("The statement finished before its timeout.")
    assert response is not None
    assert response.status_code == 503
    assert set(response.data) == {"code", "message", "request_id"}
    assert response.data["code"] == "request_timeout"
    [record] = [record for record in pyracast_logs.records if record.name == "pyracast.api"]
    assert record.levelno == logging.ERROR
    assert f"request_id={response.data['request_id']}" in record.getMessage()
