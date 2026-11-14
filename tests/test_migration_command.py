from io import StringIO
from typing import Any, cast

import psycopg
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, connection, transaction

from pyracast.accounts.management.commands.migrate import MIGRATION_LOCK, migration_session

pytestmark = pytest.mark.django_db(transaction=True)


def session_settings() -> tuple[str, str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_setting('lock_timeout'), current_setting('statement_timeout')"
        )
        return cast(tuple[str, str], cursor.fetchone())


def assert_migration_lock_released_after_close() -> None:
    with psycopg.connect(**connection.get_connection_params(), autocommit=True) as contender:
        contender.execute("SET lock_timeout = '5s'")
        contender.execute("SELECT pg_advisory_lock(%s,%s)", MIGRATION_LOCK)
        assert contender.execute("SELECT pg_advisory_unlock(%s,%s)", MIGRATION_LOCK).fetchone() == (
            True,
        )


def test_standard_migrate_command_remains_idempotent() -> None:
    output = StringIO()
    call_command("migrate", interactive=False, verbosity=1, stdout=output)
    assert "No migrations to apply" in output.getvalue()
    call_command("migrate", check_unapplied=True, verbosity=0)
    call_command("migrate", plan=True, verbosity=0)


def test_migration_lock_survives_transaction_commits_and_restores_timeouts() -> None:
    before = session_settings()
    with psycopg.connect(**connection.get_connection_params(), autocommit=True) as contender:
        with migration_session(connection, lock_timeout=2, statement_timeout=45):
            assert session_settings() == ("2s", "45s")
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("SELECT 1")
            assert contender.execute(
                "SELECT pg_try_advisory_lock(%s,%s)", MIGRATION_LOCK
            ).fetchone() == (False,)
        assert contender.execute(
            "SELECT pg_try_advisory_lock(%s,%s)", MIGRATION_LOCK
        ).fetchone() == (True,)
        assert contender.execute("SELECT pg_advisory_unlock(%s,%s)", MIGRATION_LOCK).fetchone() == (
            True,
        )
    assert session_settings() == before


@pytest.mark.parametrize("close_owner", [False, True])
def test_another_migration_is_rejected_before_schema_work(close_owner: bool) -> None:
    with psycopg.connect(**connection.get_connection_params(), autocommit=True) as owner:
        owner.execute("SELECT pg_advisory_lock(%s,%s)", MIGRATION_LOCK)
        with pytest.raises(CommandError, match="Another Pyracast migration"):
            call_command("migrate", interactive=False, verbosity=0)
        # Client close does not wait for PostgreSQL to end the lock-owning session.
        assert owner.execute("SELECT pg_advisory_unlock(%s,%s)", MIGRATION_LOCK).fetchone() == (
            True,
        )
        if close_owner:
            owner.close()
        call_command("migrate", check_unapplied=True, verbosity=0)


def test_failed_migration_releases_lock_and_restores_session() -> None:
    before = session_settings()
    with (
        pytest.raises(RuntimeError, match="migration failed"),
        migration_session(connection, lock_timeout=2, statement_timeout=45),
    ):
        raise RuntimeError("migration failed")
    assert session_settings() == before
    with psycopg.connect(**connection.get_connection_params(), autocommit=True) as contender:
        assert contender.execute(
            "SELECT pg_try_advisory_lock(%s,%s)", MIGRATION_LOCK
        ).fetchone() == (True,)
        assert contender.execute("SELECT pg_advisory_unlock(%s,%s)", MIGRATION_LOCK).fetchone() == (
            True,
        )


def test_failed_cleanup_closes_connection_and_releases_lock() -> None:
    def fail_unlock(execute: Any, sql: str, params: Any, many: bool, context: Any) -> Any:
        if "pg_advisory_unlock" in sql:
            raise DatabaseError("cleanup failed")
        return execute(sql, params, many, context)

    with (
        connection.execute_wrapper(fail_unlock),
        pytest.raises(DatabaseError, match="cleanup failed"),
        migration_session(connection, lock_timeout=2, statement_timeout=45),
    ):
        pass
    assert connection.connection is None
    assert_migration_lock_released_after_close()


def test_sql_failure_rolls_back_before_restoring_migration_session() -> None:
    before = session_settings()
    with (
        pytest.raises(DatabaseError, match="division by zero"),
        migration_session(connection, lock_timeout=2, statement_timeout=45),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute("SELECT 1 / 0")
    assert session_settings() == before


def test_schema_lock_wait_is_bounded() -> None:
    before = session_settings()
    with psycopg.connect(**connection.get_connection_params()) as owner:
        owner.execute("LOCK TABLE accounts_tenant IN ACCESS EXCLUSIVE MODE")
        with (
            pytest.raises(DatabaseError, match="lock timeout"),
            migration_session(connection, lock_timeout=1, statement_timeout=45),
            connection.cursor() as cursor,
        ):
            cursor.execute("SELECT count(*) FROM accounts_tenant")
    assert session_settings() == before


@pytest.mark.parametrize("timeout", [0, -1, 2_147_484])
def test_invalid_migration_timeout_rejected(timeout: int) -> None:
    with pytest.raises(CommandError, match="timeouts must be"):
        call_command("migrate", lock_timeout_seconds=timeout, verbosity=0)


def test_migration_refuses_enclosing_transaction() -> None:
    with (
        transaction.atomic(),
        pytest.raises(CommandError, match="outside a transaction"),
        migration_session(connection, lock_timeout=2, statement_timeout=45),
    ):
        pytest.fail("An outer transaction cannot wrap non-atomic migrations")


@pytest.mark.parametrize(("vendor", "version"), [("sqlite", 180006), ("postgresql", 170000)])
def test_unsupported_database_rejected(vendor: str, version: int) -> None:
    class UnsupportedDatabase:
        pass

    database: Any = UnsupportedDatabase()
    database.vendor = vendor
    database.pg_version = version
    database.settings_dict = {"OPTIONS": {}}
    with (
        pytest.raises(CommandError, match="PostgreSQL 18"),
        migration_session(database, lock_timeout=2, statement_timeout=45),
    ):
        pytest.fail("Unsupported database accepted")


def test_lost_migration_connection_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    database = connection
    with migration_session(database, lock_timeout=2, statement_timeout=45):
        monkeypatch.setattr(database, "is_usable", lambda: False)
    assert database.connection is None
    assert_migration_lock_released_after_close()


@pytest.mark.parametrize("reconnect", [False, True])
def test_migration_cannot_continue_after_losing_the_locked_connection(reconnect: bool) -> None:
    def replace_connection() -> None:
        connection.close()
        if reconnect:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")

    with (
        pytest.raises(CommandError, match="session lock was lost"),
        migration_session(connection, lock_timeout=2, statement_timeout=45),
    ):
        replace_connection()
    assert connection.connection is None
    assert_migration_lock_released_after_close()


@pytest.mark.parametrize("pool", [True, {}])
def test_migration_rejects_django_connection_pool(
    monkeypatch: pytest.MonkeyPatch, pool: bool | dict[str, object]
) -> None:
    monkeypatch.setitem(connection.settings_dict["OPTIONS"], "pool", pool)
    with (
        pytest.raises(CommandError, match="without connection pooling"),
        migration_session(connection, lock_timeout=2, statement_timeout=45),
    ):
        pytest.fail("Session lock cannot migrate through a pool")
