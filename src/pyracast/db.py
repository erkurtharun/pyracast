from collections.abc import Iterator
from contextlib import contextmanager

from django.db import connection, models, transaction
from django.db.transaction import TransactionManagementError


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
