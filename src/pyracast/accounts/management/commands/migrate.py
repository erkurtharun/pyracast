from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, cast

from django.core.management.base import CommandError, CommandParser
from django.core.management.commands.migrate import Command as DjangoMigrateCommand
from django.db import connections
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.backends.postgresql.base import DatabaseWrapper

# Session locks survive the commits between atomic and non-atomic migrations.
MIGRATION_LOCK = (0x50595241, 0x4D494752)


@contextmanager
def migration_session(
    connection: BaseDatabaseWrapper, *, lock_timeout: int, statement_timeout: int
) -> Iterator[None]:
    if any(value < 1 or value > 2_147_483 for value in (lock_timeout, statement_timeout)):
        raise CommandError("Migration timeouts must be between 1 and 2147483 seconds.")
    if connection.settings_dict["OPTIONS"].get("pool") not in (None, False):
        raise CommandError("Run migrations through a direct connection without connection pooling.")
    if connection.vendor != "postgresql" or cast(DatabaseWrapper, connection).pg_version < 180000:
        raise CommandError("Pyracast migrations require PostgreSQL 18 or newer.")
    if not connection.get_autocommit():
        raise CommandError(
            "Run migrations outside a transaction, using a direct database connection."
        )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_setting('lock_timeout'), current_setting('statement_timeout')"
        )
        previous = cursor.fetchone()
        cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", MIGRATION_LOCK)
        if not cursor.fetchone()[0]:
            raise CommandError("Another Pyracast migration is running. Retry after it completes.")
    locked_connection = connection.connection

    def require_locked_connection(
        execute: Callable[..., Any], sql: str, params: Any, many: bool, context: Any
    ) -> Any:
        if connection.connection is not locked_connection:
            raise CommandError("Migration connection changed; its session lock was lost.")
        return execute(sql, params, many, context)

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('lock_timeout', %s, false), "
                "set_config('statement_timeout', %s, false)",
                [f"{lock_timeout}s", f"{statement_timeout}s"],
            )
        with connection.execute_wrapper(require_locked_connection):
            yield
        if connection.connection is not locked_connection:
            raise CommandError("Migration connection changed; its session lock was lost.")
    finally:
        try:
            if connection.connection is locked_connection and connection.is_usable():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config('lock_timeout', %s, false), "
                        "set_config('statement_timeout', %s, false), pg_advisory_unlock(%s, %s)",
                        [*previous, *MIGRATION_LOCK],
                    )
            else:
                connection.close()
        except BaseException:
            connection.close()
            raise


class Command(DjangoMigrateCommand):
    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)
        parser.add_argument("--lock-timeout-seconds", type=int, default=5)
        parser.add_argument("--statement-timeout-seconds", type=int, default=900)

    def handle(self, *args: Any, **options: Any) -> str | None:
        with migration_session(
            connections[options["database"]],
            lock_timeout=options["lock_timeout_seconds"],
            statement_timeout=options["statement_timeout_seconds"],
        ):
            return super().handle(*args, **options)
