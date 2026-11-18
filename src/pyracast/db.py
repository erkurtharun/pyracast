from collections.abc import Collection, Iterator
from contextlib import contextmanager

from django.apps.registry import Apps
from django.db import connection, migrations, models, transaction
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.transaction import TransactionManagementError


def refuse_values_outside(
    model: str, lookup: str, allowed: Collection[str], *, scope: models.Q | None = None
) -> migrations.RunPython:
    """Fail the migration before a vocabulary CHECK is added while stored rows violate it.

    The migration is atomic, so the failure changes nothing; no row is rewritten or deleted.
    """

    def refuse(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
        rows = apps.get_model(model)._default_manager.using(schema_editor.connection.alias)
        found = sorted(
            {
                repr(value)
                for value in rows.filter(scope or models.Q())
                .exclude(**{f"{lookup}__in": list(allowed)})
                .values_list(lookup, flat=True)
                .distinct()
            }
        )
        if found:
            table = rows.model._meta.db_table
            raise ValueError(
                f"{table}.{lookup} holds values outside its vocabulary: {', '.join(found)}. "
                "Correct these rows and migrate again; nothing was changed."
            )

    return migrations.RunPython(refuse, migrations.RunPython.noop)


def choices_check(
    field: str, choices: type[models.TextChoices], *, name: str = ""
) -> models.CheckConstraint:
    return models.CheckConstraint(
        condition=models.Q(**{f"{field}__in": choices.values}),
        name=name or f"%(class)s_{field}_chk",
    )


@contextmanager
def read_snapshot() -> Iterator[None]:
    """Run the block in one REPEATABLE READ, READ ONLY transaction: every query sees the
    same committed state, takes no row lock and cannot write."""
    if connection.in_atomic_block:
        # PostgreSQL accepts the isolation level only as the transaction's first statement.
        raise TransactionManagementError("A read snapshot must open its own transaction.")
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        yield
