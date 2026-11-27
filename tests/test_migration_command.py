import itertools
from collections.abc import Iterator
from contextlib import contextmanager
from io import StringIO
from typing import Any, cast

import psycopg
import pytest
from django.apps import apps
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, connection, transaction
from django.db.migrations.loader import MigrationLoader
from django.db.models.signals import pre_migrate

from pyracast.accounts.management.commands.migrate import MIGRATION_LOCK, migration_session
from pyracast.migration_history import HISTORY

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


def test_the_project_migrations_on_disk_are_the_recorded_history_in_chain_order() -> None:
    loader = MigrationLoader(None)
    disk = loader.disk_migrations
    configs = [config for config in apps.get_app_configs() if config.name.startswith("pyracast.")]
    project = {config.label for config in configs if any(config.get_models())}
    assert {label for label, _ in disk} & {config.label for config in configs} == project
    baseline = {app: {"0001_initial"} for app in project}
    # Regions and connections reference each other; Django breaks that cycle in a second file.
    baseline["connections"].add("0002_initial")
    assert {
        app: {
            name for (label, name), migration in disk.items() if label == app and migration.initial
        }
        for app in project
    } == baseline
    assert len(set(HISTORY)) == len(HISTORY) == 69
    assert sorted(key for key in disk if key[0] in project) == sorted(HISTORY)
    for app in project:
        chain = [key for key in HISTORY if key[0] == app]
        for previous, key in itertools.pairwise(chain):
            assert previous in disk[key].dependencies
        assert loader.graph.leaf_nodes(app) == [chain[-1]]


@contextmanager
def ledger_rows(*rows: tuple[str, str]) -> Iterator[None]:
    with connection.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO django_migrations (app, name, applied) VALUES (%s, %s, now())", rows
        )
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.executemany("DELETE FROM django_migrations WHERE app = %s AND name = %s", rows)


@pytest.mark.parametrize(
    "options", [{"plan": True}, {"check_unapplied": True}, {"interactive": False}]
)
def test_migrate_refuses_a_ledger_that_names_project_migrations_missing_from_disk(
    options: dict[str, bool],
) -> None:
    started: list[object] = []

    def record(**kwargs: object) -> None:
        started.append(kwargs)

    pre_migrate.connect(record, dispatch_uid="fence-test")
    try:
        with (
            ledger_rows(("plants", "0099_replaced_history"), ("accounts", "0099_replaced")),
            pytest.raises(CommandError) as refusal,
        ):
            call_command("migrate", verbosity=0, **options)
    finally:
        pre_migrate.disconnect(dispatch_uid="fence-test")
    message = str(refusal.value)
    assert "accounts.0099_replaced, plants.0099_replaced_history" in message
    assert "deploy/reset_database.py" in message
    assert started == []
    assert_migration_lock_released_after_close()
    call_command("migrate", check_unapplied=True, verbosity=0)


def test_migrate_ignores_ledger_rows_of_other_apps_missing_from_disk() -> None:
    with ledger_rows(("auth", "9999_removed_upstream")):
        call_command("migrate", plan=True, verbosity=0)
        call_command("migrate", check_unapplied=True, verbosity=0)


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


def idle_in_transaction_setting() -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('idle_in_transaction_session_timeout')")
        return cast(tuple[str], cursor.fetchone())[0]


def test_runtime_sessions_end_a_transaction_left_idle() -> None:
    assert idle_in_transaction_setting() == "2min"


def test_migration_session_sets_its_own_idle_transaction_bound_and_restores_it() -> None:
    with migration_session(connection, lock_timeout=2, statement_timeout=45):
        assert idle_in_transaction_setting() == "0"
    assert idle_in_transaction_setting() == "2min"
    observed: list[str] = []

    def fail_after_reading() -> None:
        observed.append(idle_in_transaction_setting())
        raise RuntimeError("migration failed")

    with (
        pytest.raises(RuntimeError, match="migration failed"),
        migration_session(
            connection, lock_timeout=2, statement_timeout=45, idle_in_transaction_timeout=30
        ),
    ):
        fail_after_reading()
    assert observed == ["30s"]
    assert idle_in_transaction_setting() == "2min"


def test_migrate_command_takes_an_idle_transaction_bound() -> None:
    call_command("migrate", idle_in_transaction_timeout_seconds=0, verbosity=0)
    with pytest.raises(CommandError, match="timeouts must be"):
        call_command("migrate", idle_in_transaction_timeout_seconds=-1, verbosity=0)
    assert idle_in_transaction_setting() == "2min"
