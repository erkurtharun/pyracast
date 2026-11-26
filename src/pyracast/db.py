from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager

from django.apps.registry import Apps
from django.db import IntegrityError, connection, migrations, models, transaction
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.operations.base import Operation
from django.db.migrations.state import ProjectState
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


def refuse_rows(
    model: str, violating: Callable[[type[models.Model]], models.Q], rule: str
) -> migrations.RunPython:
    """Fail the atomic migration before a constraint is added while stored rows break it; no
    row is rewritten or deleted."""

    def refuse(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
        stored = apps.get_model(model)
        rows = stored._default_manager.using(schema_editor.connection.alias)
        found = sorted(rows.filter(violating(stored)).values_list("pk", flat=True))
        if found:
            raise ValueError(
                f"{stored._meta.db_table} holds {len(found)} row(s) that break the rule "
                f"'{rule}': ids {found}. Correct these rows and migrate again; nothing was changed."
            )

    return migrations.RunPython(refuse, migrations.RunPython.noop)


class RenameInPlace(Operation):
    """Rename a model constraint or index with ALTER ... RENAME: nothing is dropped, rebuilt or
    validated again. `item` is the object as the model now declares it; it differs from the
    stored one only in its name and in columns an earlier RenameField renamed, which
    PostgreSQL follows by itself."""

    reduces_to_sql = True

    def __init__(
        self, model_name: str, old_name: str, item: models.Index | models.BaseConstraint
    ) -> None:
        self.model_name = model_name
        self.old_name = old_name
        self.item = item

    def state_forwards(self, app_label: str, state: ProjectState) -> None:
        model = state.models[app_label, self.model_name.lower()]
        option = "indexes" if isinstance(self.item, models.Index) else "constraints"
        items = list(model.options[option])
        position = next(index for index, item in enumerate(items) if item.name == self.old_name)
        items[position] = self.item
        model.options[option] = items
        state.reload_model(app_label, self.model_name.lower(), delay=True)

    def database_forwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        model = to_state.apps.get_model(app_label, self.model_name)
        self.rename(schema_editor, model, self.old_name, self.item.name)

    def database_backwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        model = to_state.apps.get_model(app_label, self.model_name)
        self.rename(schema_editor, model, self.item.name, self.old_name)

    def rename(
        self,
        schema_editor: BaseDatabaseSchemaEditor,
        model: type[models.Model],
        old: str,
        new: str,
    ) -> None:
        table = schema_editor.quote_name(model._meta.db_table)
        # Locked first, so autovacuum cannot hold the table while it waits for this transaction.
        schema_editor.execute(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE")
        schema_editor.execute(self.rename_sql(schema_editor, model, old, new))

    def rename_sql(
        self,
        schema_editor: BaseDatabaseSchemaEditor,
        model: type[models.Model],
        old: str,
        new: str,
    ) -> str:
        quote = schema_editor.quote_name
        item = self.item
        # Django builds these as indexes, which have no row in pg_constraint.
        if isinstance(item, models.Index) or (
            isinstance(item, models.UniqueConstraint)
            and (item.condition is not None or item.contains_expressions)
        ):
            return f"ALTER INDEX {quote(old)} RENAME TO {quote(new)}"
        table = quote(model._meta.db_table)
        return f"ALTER TABLE {table} RENAME CONSTRAINT {quote(old)} TO {quote(new)}"

    def describe(self) -> str:
        return f"Rename {self.old_name} on {self.model_name} to {self.item.name}"


def fire_deferred_checks(schema_editor: BaseDatabaseSchemaEditor) -> None:
    """PostgreSQL refuses ALTER TABLE on a table with pending deferred-key events, so a data
    repair runs them before the migration adds its constraint."""
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def _lease_rule(leased: Collection[str], kept: Collection[str]) -> models.Q:
    held = models.Q(lease_token__isnull=False, lease_expires_at__isnull=False)
    free = models.Q(lease_token__isnull=True, lease_expires_at__isnull=True)
    leased, kept = [str(status) for status in leased], [str(status) for status in kept]
    return (held & models.Q(status__in=[*leased, *kept])) | (free & ~models.Q(status__in=leased))


def lease_check(leased: Collection[str], *, kept: Collection[str] = ()) -> models.CheckConstraint:
    """A lease is held, with both columns, by every row in a leased status; a kept status may
    still hold the lease of its last owner; every other status holds none."""
    return models.CheckConstraint(condition=_lease_rule(leased, kept), name="%(class)s_lease_chk")


def refuse_lease_breaches(
    model: str, leased: Collection[str], *, kept: Collection[str] = ()
) -> migrations.RunPython:
    return refuse_rows(
        model,
        lambda _: ~_lease_rule(leased, kept),
        "both lease columns are held together, always by a leased status and otherwise only "
        "by a kept one",
    )


def choices_check(
    field: str, choices: type[models.TextChoices], *, name: str = "", blank: bool = False
) -> models.CheckConstraint:
    values = ["", *choices.values] if blank else choices.values
    return models.CheckConstraint(
        condition=models.Q(**{f"{field}__in": values}),
        name=name or f"%(class)s_{field}_chk",
    )


class JsonType(models.Func):
    function = "jsonb_typeof"
    output_field = models.TextField()


class JsonPathExists(models.Func):
    # Strict paths raise on a value of another structure; the trailing `true` turns that into
    # NULL, so the type test beside the path decides.
    template = "jsonb_path_exists(%(expressions)s::jsonpath, '{}', true)"
    output_field = models.BooleanField()


TIME_ITEMS = (
    'strict $[*] ? (@.type() != "string" || !(@ like_regex "^([01][0-9]|2[0-3]):[0-5][0-9]$"))'
)


def json_shape(field: str, kind: str, breach: str = "") -> models.Q:
    """The JSON value has the type `kind` and nothing matches the strict jsonpath `breach`."""
    shape = models.Q(models.lookups.Exact(JsonType(field), kind))
    return shape & ~models.Q(JsonPathExists(field, models.Value(breach))) if breach else shape


def json_shape_check(field: str, kind: str, breach: str = "") -> models.CheckConstraint:
    return models.CheckConstraint(
        condition=json_shape(field, kind, breach), name=f"%(class)s_{field}_chk"
    )


def violated_constraint(error: IntegrityError) -> str | None:
    name = getattr(getattr(error.__cause__, "diag", None), "constraint_name", None)
    return name if isinstance(name, str) else None


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
