import pytest
from django.db import connections


@pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
def test_test_sessions_do_not_wait_for_wal_flush() -> None:
    for alias in ("default", "auth_cache"):
        with connections[alias].cursor() as cursor:
            cursor.execute("SHOW synchronous_commit")
            assert cursor.fetchone() == ("off",)
