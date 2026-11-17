from typing import Any

import pytest
from django.apps import apps
from django.db import connection, models

from pyracast.schema_objects import SCHEMA_OBJECTS, CompositeForeignKey, Extension, RowTrigger

pytestmark = pytest.mark.django_db(transaction=True)


def project_tables() -> list[str]:
    return sorted(
        {
            model._meta.db_table
            for model in apps.get_models(include_auto_created=True)
            if model.__module__.startswith("pyracast.")
        }
    )


def foreign_keys() -> list[tuple[Any, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT c.conname, child.relname,
                ARRAY(
                    SELECT a.attname::text
                    FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, position)
                    JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
                    ORDER BY k.position
                ), parent.relname,
                ARRAY(
                    SELECT a.attname::text
                    FROM unnest(c.confkey) WITH ORDINALITY AS k(attnum, position)
                    JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.attnum
                    ORDER BY k.position
                ), c.convalidated, c.condeferrable, c.condeferred
            FROM pg_constraint c
            JOIN pg_class child ON child.oid = c.conrelid
            JOIN pg_namespace ns ON ns.oid = child.relnamespace
            JOIN pg_class parent ON parent.oid = c.confrelid
            WHERE c.contype = 'f' AND ns.nspname = current_schema()
                AND child.relname::text = ANY(%s)
            """,
            (project_tables(),),
        )
        return list(cursor.fetchall())


def test_composite_foreign_keys_equal_the_declared_schema_objects() -> None:
    declared = {
        item.name: (item.table, item.columns, item.target, item.target_columns)
        for item in SCHEMA_OBJECTS
        if isinstance(item, CompositeForeignKey)
    }
    actual = {
        name: (child, tuple(columns), parent, tuple(targets))
        for name, child, columns, parent, targets, *_ in foreign_keys()
        if len(columns) > 1
    }
    assert actual == declared


def test_every_foreign_key_is_validated_and_initially_deferred() -> None:
    rows = foreign_keys()
    model_foreign_keys = {
        (
            model._meta.db_table,
            (field.column,),
            field.target_field.model._meta.db_table,
            (field.target_field.column,),
        )
        for model in apps.get_models(include_auto_created=True)
        if model.__module__.startswith("pyracast.")
        for field in model._meta.local_fields
        if isinstance(field, models.ForeignKey)
        and field.deconstruct()[3].get("db_constraint", True)
    }
    actual = {
        (child, tuple(columns), parent, tuple(targets))
        for _, child, columns, parent, targets, *_ in rows
    }
    assert model_foreign_keys <= actual
    assert all(
        validated and deferred and initially_deferred
        for *_, validated, deferred, initially_deferred in rows
    )


def test_triggers_and_their_functions_equal_the_declared_schema_objects() -> None:
    declared = {
        (
            item.table,
            item.name,
            f"CREATE TRIGGER {item.name} {item.timing} ON public.{item.table} "
            f"FOR EACH ROW EXECUTE FUNCTION {item.function}()",
            item.function,
            item.body,
        )
        for item in SCHEMA_OBJECTS
        if isinstance(item, RowTrigger)
    }
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid), p.proname, p.prosrc
            FROM pg_trigger t
            JOIN pg_class c ON c.oid = t.tgrelid
            JOIN pg_namespace ns ON ns.oid = c.relnamespace
            JOIN pg_proc p ON p.oid = t.tgfoid
            WHERE ns.nspname = current_schema() AND NOT t.tgisinternal
            """
        )
        triggers = set(cursor.fetchall())
        cursor.execute(
            """
            SELECT p.proname
            FROM pg_proc p
            JOIN pg_namespace ns ON ns.oid = p.pronamespace
            WHERE ns.nspname = current_schema() AND NOT EXISTS (
                SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e'
            )
            """
        )
        functions = {name for (name,) in cursor.fetchall()}
    assert triggers == declared
    assert functions == {item[3] for item in declared}


def table_columns(table: str) -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = %s
            """,
            (table,),
        )
        return {name for (name,) in cursor.fetchall()}


def test_forecasts_are_archived_only_as_issuances_for_horizontal_bindings() -> None:
    assert table_columns("plants_forecastissuance")
    assert not table_columns("plants_plantforecast")
    assert "tilt_degrees" not in table_columns("connections_sitebinding")


def test_plants_and_reminders_are_stored_in_their_one_current_shape() -> None:
    assert "deleted_at" not in table_columns("plants_plant")
    assert "retired_at" not in table_columns("notifications_reminderschedule")
    assert "grouped" not in table_columns("notifications_notificationdelivery")


def test_extensions_equal_the_declared_schema_objects() -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT extname FROM pg_extension WHERE extname <> 'plpgsql'")
        installed = {name for (name,) in cursor.fetchall()}
    assert installed == {item.name for item in SCHEMA_OBJECTS if isinstance(item, Extension)}
