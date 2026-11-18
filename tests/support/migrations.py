from typing import Any

from django.db import connection
from django.db.migrations.executor import MigrationExecutor


def leaf_nodes() -> list[tuple[str, str]]:
    return list(MigrationExecutor(connection).loader.graph.leaf_nodes())


def migrate(*targets: tuple[str, str]) -> None:
    MigrationExecutor(connection).migrate(list(targets))


def forward() -> None:
    migrate(*leaf_nodes())


def constraints(table: str) -> dict[str, Any]:
    with connection.cursor() as cursor:
        return dict(connection.introspection.get_constraints(cursor, table))


def checks(table: str) -> set[str]:
    return {name for name, details in constraints(table).items() if details["check"]}


def triggers() -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace ns ON ns.oid = c.relnamespace "
            "WHERE ns.nspname = current_schema() AND NOT t.tgisinternal"
        )
        return {name for (name,) in cursor.fetchall()}
