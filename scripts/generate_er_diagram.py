import argparse
import os
import re
import sys
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

import django
from django.apps import apps
from django.apps.registry import Apps
from django.db import connection, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations import Migration
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.operations import RunSQL, SeparateDatabaseAndState
from django.db.migrations.operations.base import Operation

OUTPUT = Path(__file__).resolve().parents[1] / "docs" / "DATA_MODEL.md"
COMMAND = "uv run python scripts/generate_er_diagram.py"
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MIGRATION_DDL = re.compile(
    r"\b(?:ADD CONSTRAINT\b|(?:CREATE|DROP) (?!FUNCTION\b)\w)", re.IGNORECASE
)
_MIGRATION_FOREIGN_KEY = re.compile(
    r"ALTER TABLE (\w+) ADD CONSTRAINT (\w+) (FOREIGN KEY [^;]+?)( NOT VALID)?\s*(?:;|$)",
    re.IGNORECASE,
)
_MIGRATION_TRIGGER = re.compile(
    r"CREATE TRIGGER (\w+) ([^;]+?) ON (\w+) ([^;]+?)\s*(?:;|$)", re.IGNORECASE
)
_MIGRATION_VALIDATION = re.compile(r"VALIDATE CONSTRAINT (\w+)", re.IGNORECASE)

type Field = models.Field[Any, Any]
type Rules = dict[str, list[tuple[str, str]]]


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    keys: tuple[str, ...]
    notes: tuple[str, ...]


@dataclass(frozen=True)
class Relationship:
    parent: str
    child: str
    cardinality: str
    column: str


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[Column, ...]
    relationships: tuple[Relationship, ...]
    rules: tuple[str, ...]


@dataclass(frozen=True)
class App:
    label: str
    module: str
    tables: tuple[Table, ...]


def token(text: str) -> str:
    result = re.sub(r"[^A-Za-z0-9_]+", "_", text.replace("[]", "_array")).strip("_")
    if not _TOKEN.fullmatch(result):
        raise ValueError(f"Cannot express {text!r} as a Mermaid token.")
    return result


def unique_tokens(names: list[str], scope: str) -> None:
    if len(set(names)) != len(names):
        raise ValueError(f"Distinct names collapse to the same Mermaid token in {scope}.")


@contextmanager
def offline() -> Iterator[None]:
    refusal = RuntimeError("The data model must be generated without a database connection.")
    with mock.patch.object(connection, "connect", side_effect=refusal):
        yield


def column_name(field: Field) -> str:
    if field.column is None:
        raise ValueError(f"{field} has no database column.")
    return field.column


def column(field: Field) -> Column:
    database_type = field.db_type(connection)
    if database_type is None:
        raise ValueError(f"{field} has no database column type.")
    keys = ["PK"] if field.primary_key else []
    notes = ["nullable"] if field.null else []
    if isinstance(field, models.ForeignKey):
        keys.append("FK")
        target = field.target_field
        notes.append(f"references {target.model._meta.db_table}.{target.column}")
    if field.unique and not field.primary_key:
        keys.append("UK")
    return Column(token(column_name(field)), token(database_type), tuple(keys), tuple(notes))


def relationship(field: models.ForeignKey[Any, Any], child: str) -> Relationship:
    parent_side = "|o" if field.null else "||"
    child_side = "o|" if field.unique else "o{"
    parent = token(field.target_field.model._meta.db_table)
    return Relationship(parent, child, f"{parent_side}--{child_side}", column_name(field))


def readable_sql(statement: object) -> str:
    return "".join(
        character
        if character == " " or (character.isprintable() and not character.isspace())
        else f"\\u{ord(character):04x}"
        for character in str(statement)
    )


def definition(
    item: models.BaseConstraint | models.Index,
    model: type[models.Model],
    editor: BaseDatabaseSchemaEditor,
) -> str:
    sql = readable_sql(item.create_sql(model, editor))
    table = editor.quote_name(model._meta.db_table)
    name = editor.quote_name(str(item.name))
    prefixes = {
        f"ALTER TABLE {table} ADD CONSTRAINT {name} ": "",
        f"CREATE UNIQUE INDEX {name} ON {table} ": "UNIQUE INDEX ",
        f"CREATE INDEX {name} ON {table} ": "INDEX ",
    }
    for prefix, replacement in prefixes.items():
        if sql.startswith(prefix):
            return replacement + sql.removeprefix(prefix)
    return sql


def unique_together(
    model: type[models.Model], names: tuple[str, ...], editor: BaseDatabaseSchemaEditor
) -> str:
    columns = []
    for name in names:
        field = model._meta.get_field(name)
        if not isinstance(field, models.Field):
            raise TypeError(f"{model._meta.label}.{name} is not a concrete field.")
        columns.append(editor.quote_name(column_name(field)))
    return f"UNIQUE ({', '.join(columns)})"


def forward_sql(operations: Iterable[Operation]) -> Iterator[str]:
    for operation in operations:
        if isinstance(operation, SeparateDatabaseAndState):
            yield from forward_sql(operation.database_operations)
        elif isinstance(operation, RunSQL):
            sql = operation.sql
            for statement in [sql] if isinstance(sql, str) else sql:
                yield " ".join((statement if isinstance(statement, str) else statement[0]).split())


def migration_script(migrations: dict[tuple[str, str], Migration]) -> str:
    return ";".join(
        sql for key in sorted(migrations) for sql in forward_sql(migrations[key].operations)
    )


def migration_rules(script: str) -> Rules:
    validated = set(_MIGRATION_VALIDATION.findall(script))
    found = [
        (table_name, name, sql + (pending if name not in validated else ""))
        for table_name, name, sql, pending in _MIGRATION_FOREIGN_KEY.findall(script)
    ]
    found += [
        (table_name, name, f"TRIGGER {timing} {action}")
        for name, timing, table_name, action in _MIGRATION_TRIGGER.findall(script)
    ]
    # Unread DDL would leave the document silently incomplete.
    if len(found) != len(_MIGRATION_DDL.findall(script)):
        raise ValueError("A RunSQL migration changes the schema in a way this script cannot read.")
    grouped: Rules = {}
    for table_name, name, sql in found:
        grouped.setdefault(table_name, []).append((name, sql))
    return grouped


def rules(
    model: type[models.Model], editor: BaseDatabaseSchemaEditor, migrated: list[tuple[str, str]]
) -> tuple[str, ...]:
    meta = model._meta
    named = sorted(
        [
            *(
                (str(item.name), definition(item, model, editor))
                for item in [*meta.constraints, *meta.indexes]
            ),
            *migrated,
        ]
    )
    unnamed = sorted(
        unique_together(model, tuple(fields), editor) for fields in meta.unique_together
    )
    checks = sorted(
        f"CHECK ({check})"
        for field in meta.local_concrete_fields
        if (check := field.db_check(connection))
    )
    return (
        *(f"- {code(name)}: {code(sql)}" for name, sql in named),
        *(f"- `unique_together` (name generated by Django): {code(sql)}" for sql in unnamed),
        *(f"- column check (name generated by PostgreSQL): {code(sql)}" for sql in checks),
    )


def table(
    model: type[models.Model], editor: BaseDatabaseSchemaEditor, migrated: list[tuple[str, str]]
) -> Table:
    meta = model._meta
    name = token(meta.db_table)
    fields = list(meta.local_concrete_fields)
    columns = tuple(column(field) for field in fields)
    unique_tokens([item.name for item in columns], meta.db_table)
    relationships = tuple(
        relationship(field, name) for field in fields if isinstance(field, models.ForeignKey)
    )
    return Table(name, columns, relationships, rules(model, editor, migrated))


def table_models(registry: Apps) -> list[type[models.Model]]:
    order = {config.label: index for index, config in enumerate(apps.get_app_configs())}

    def position(model: type[models.Model]) -> tuple[bool, int, str]:
        meta = model._meta
        project = apps.get_app_config(meta.app_label).name.startswith("pyracast.")
        return (not project, order[meta.app_label], meta.db_table)

    selected = [
        model
        for model in registry.get_models(include_auto_created=True)
        if model._meta.managed and not model._meta.proxy
    ]
    return sorted(selected, key=position)


def collect() -> tuple[App, ...]:
    grouped: dict[str, list[Table]] = {}
    with offline():
        editor = connection.schema_editor(collect_sql=True, atomic=False)
        loader = MigrationLoader(None)
        migrated = migration_rules(migration_script(loader.disk_migrations))
        # The migration state is what `migrate` builds; imported models give the keys of
        # third-party many-to-many tables another type.
        for model in table_models(loader.project_state().apps):
            meta = model._meta
            grouped.setdefault(meta.app_label, []).append(
                table(model, editor, migrated.pop(meta.db_table, []))
            )
    if migrated:
        raise ValueError(f"Migrations constrain tables without a model: {sorted(migrated)}.")
    unique_tokens([item.name for tables in grouped.values() for item in tables], "tables")
    return tuple(
        App(label, apps.get_app_config(label).name, tuple(tables))
        for label, tables in grouped.items()
    )


def entity(item: Table) -> list[str]:
    lines = [f"    {item.name} {{"]
    for attribute in item.columns:
        keys = f" {', '.join(attribute.keys)}" if attribute.keys else ""
        notes = f' "{"; ".join(attribute.notes)}"' if attribute.notes else ""
        lines.append(f"        {attribute.type} {attribute.name}{keys}{notes}")
    lines.append("    }")
    return lines


def link(item: Relationship) -> str:
    return f'    {item.parent} {item.cardinality} {item.child} : "{item.column}"'


def code(text: str) -> str:
    fence = "``" if "`" in text else "`"
    padding = " " if fence == "``" else ""
    return f"{fence}{padding}{text}{padding}{fence}"


def render(groups: tuple[App, ...]) -> str:
    tables = [item for group in groups for item in group.tables]
    relationships = [relation for item in tables for relation in item.relationships]
    lines = [
        "# Data model",
        "",
        "> Generated from the Django migrations; do not edit by hand. Regenerate with",
        f"> `{COMMAND}`; its `--check` option fails when this file is stale.",
        "",
        "Column types are PostgreSQL types with every character outside letters, digits and",
        "underscores replaced by `_`. Keys: `PK` primary, `FK` foreign, `UK` unique. Column",
        "comments mark nullable columns and name the referenced column of a foreign key.",
        "Relationship labels name the foreign key column on the child table. Constraint and",
        "index definitions are the DDL Django emits, with whitespace other than spaces and",
        "non-printing characters shown as `\\uXXXX` escapes. Implicit indexes on foreign key",
        "and `db_index` columns are not listed.",
        "",
        f"Foreign keys from model fields are `{connection.ops.deferrable_sql().strip()}`.",
        "Composite foreign keys and triggers exist only in `RunSQL` migrations; they are read",
        "from the migration files and listed with the constraints of their table as written",
        "there, not drawn as relationships. Schema changes made by migration Python code are",
        "not visible to this document, and `django_migrations` has no model and is not shown.",
        "",
        "## Overview",
        "",
        f"{len(tables)} tables and {len(relationships)} single-column foreign keys.",
        "",
        "```mermaid",
        "erDiagram",
        *(f"    {item.name}" for item in tables),
        *(link(item) for item in relationships),
        "```",
    ]
    for group in groups:
        lines += ["", f"## {group.label} ({code(group.module)})", "", "```mermaid", "erDiagram"]
        for item in group.tables:
            lines += entity(item)
        lines += [link(relation) for item in group.tables for relation in item.relationships]
        lines += ["```", "", "### Constraints and indexes"]
        constrained = [item for item in group.tables if item.rules]
        if not constrained:
            lines += ["", "None beyond primary keys, foreign keys and unique columns."]
        for item in constrained:
            lines += ["", f"#### {code(item.name)}", "", *item.rules]
    return "\n".join(lines) + "\n"


def generate() -> str:
    return render(collect())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    if not apps.ready:
        os.environ["DJANGO_SETTINGS_MODULE"] = "pyracast.settings"
        django.setup()
    expected = generate().encode()
    output: Path = args.output
    if args.check:
        if not output.is_file() or output.read_bytes() != expected:
            print(
                f"{output} is out of date with the Django migrations. Run: {COMMAND}",
                file=sys.stderr,
            )
            return 1
        return 0
    output.write_bytes(expected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
