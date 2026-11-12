import psycopg
import pytest
from django.db import connection, connections, transaction


@pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
def test_application_database_sessions_start_with_finite_limits() -> None:
    for alias in ("default", "auth_cache"):
        database = connections[alias]
        assert database.get_connection_params()["connect_timeout"] == 5
        with database.cursor() as cursor:
            cursor.execute(
                "SELECT current_setting('lock_timeout')::interval = interval '5 seconds', "
                "current_setting('statement_timeout')::interval = interval '60 seconds'"
            )
            assert cursor.fetchone() == (True, True)


@pytest.mark.django_db(transaction=True)
def test_postgresql_cancels_a_blocked_operation_and_the_session_can_continue() -> None:
    with psycopg.connect(**connection.get_connection_params(), autocommit=True) as contender:
        contender.execute("SET lock_timeout = '100ms'")
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(1740573, 1)")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                contender.execute("SELECT pg_advisory_xact_lock(1740573, 1)")
        assert contender.execute("SELECT 1").fetchone() == (1,)
        contender.execute("SET statement_timeout = '100ms'")
        with pytest.raises(psycopg.errors.QueryCanceled):
            contender.execute("SELECT pg_sleep(1)")
        assert contender.execute("SELECT 1").fetchone() == (1,)
