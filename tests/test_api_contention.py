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
    assert response.data == {"code": "workspace_busy"}
    assert response["Retry-After"] == "1"
    tenant.refresh_from_db()
    assert tenant.name == "Contended workspace"


@pytest.mark.parametrize("cause", [None, DeadlockDetected("unrelated database failure")])
def test_other_database_failures_are_not_classified_as_workspace_contention(
    cause: Exception | None,
) -> None:
    error = OperationalError("Database operation failed")
    error.__cause__ = cause
    assert exception_handler(error, {}) is None
