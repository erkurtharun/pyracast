from collections.abc import Collection
from typing import Any

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models import Model, QuerySet


def leaf_nodes() -> list[tuple[str, str]]:
    return list(MigrationExecutor(connection).loader.graph.leaf_nodes())


def migrate(*targets: tuple[str, str]) -> None:
    MigrationExecutor(connection).migrate(list(targets))


def row_values(rows: QuerySet[Model], fields: Collection[str] | None = None) -> dict[str, Any]:
    """The one row's values in the columns its table has at the current migration state."""
    with connection.cursor() as cursor:
        present = {
            column.name
            for column in connection.introspection.get_table_description(
                cursor, rows.model._meta.db_table
            )
        }
    names = [
        field.attname
        for field in rows.model._meta.concrete_fields
        if field.column in present and (fields is None or field.attname in fields)
    ]
    return rows.values(*names).get()


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
