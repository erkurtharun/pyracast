import re

import pytest
from django.apps import apps
from django.db import connection, models

from pyracast.schema_objects import SCHEMA_OBJECTS, CompositeForeignKey

KINDS = {"chk", "uniq", "excl", "idx", "fk"}
# Names PostgreSQL or Django give objects no model declares: primary keys, unique and positive
# columns, foreign key columns and their indexes.
GENERATED = re.compile(r"_(?:pkey|key|check)$|_[0-9a-f]{8}(?:_like|_uniq|_fk_\w+)?$")


def project_models() -> list[type[models.Model]]:
    return [
        model
        for model in apps.get_models(include_auto_created=True)
        if model.__module__.startswith("pyracast.")
    ]


def declared_names() -> dict[str, tuple[str, str]]:
    tables = {model._meta.db_table: model for model in project_models()}
    names: dict[str, tuple[str, str]] = {}
    for model in project_models():
        for item in model._meta.constraints:
            names[str(item.name)] = (model._meta.model_name or "", "constraint")
        for index in model._meta.indexes:
            names[str(index.name)] = (model._meta.model_name or "", "index")
    for schema_object in SCHEMA_OBJECTS:
        if isinstance(schema_object, CompositeForeignKey):
            model = tables[schema_object.table]
            names[schema_object.name] = (model._meta.model_name or "", "constraint")
    return names


def test_every_constraint_and_index_name_follows_the_convention() -> None:
    names = declared_names()
    breaking = sorted(
        name
        for name, (model, kind) in names.items()
        if not name.startswith(f"{model}_")
        or name.rsplit("_", 1)[-1] not in KINDS
        or name == f"{model}_{name.rsplit('_', 1)[-1]}"
        or (kind == "index") != name.endswith("_idx")
        or len(name) > (30 if kind == "index" else 63)
    )
    assert breaking == []


def built_names() -> set[str]:
    tables = [model._meta.db_table for model in project_models()]
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT c.conname FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            JOIN pg_namespace ns ON ns.oid = t.relnamespace
            WHERE ns.nspname = current_schema() AND t.relname = ANY(%s) AND c.contype <> 'n'
            UNION
            SELECT i.indexname FROM pg_indexes i
            WHERE i.schemaname = current_schema() AND i.tablename = ANY(%s)
            """,
            (tables, tables),
        )
        return {name for (name,) in cursor.fetchall()}


@pytest.mark.django_db
def test_built_constraint_and_index_names_equal_the_declared_names() -> None:
    built, declared = built_names(), set(declared_names())
    assert declared - built == set()
    assert sorted(name for name in built - declared if not GENERATED.search(name)) == []
