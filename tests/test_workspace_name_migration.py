import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

from pyracast.accounts.models import Tenant

pytestmark = pytest.mark.django_db(transaction=True)

PREVIOUS = ("accounts", "0001_initial")


def _migrate(*targets: tuple[str, str]) -> None:
    MigrationExecutor(connection).migrate(list(targets))


def _forward() -> None:
    _migrate(*MigrationExecutor(connection).loader.graph.leaf_nodes())


def _name_index() -> bool:
    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(cursor, Tenant._meta.db_table)
    return "unique_workspace_name" in constraints


def test_workspaces_may_share_a_name_after_the_migration_and_the_migration_reverses() -> None:
    try:
        _migrate(PREVIOUS)
        first = Tenant.objects.create(name="Harbor Solar")
        with pytest.raises(IntegrityError), transaction.atomic():
            Tenant.objects.create(name=" harbor solar ")
        assert _name_index()

        _forward()

        assert not _name_index()
        second = Tenant.objects.create(name=" harbor solar ")
        renamed = Tenant.objects.create(name="Bay Solar")
        Tenant.objects.filter(pk=renamed.pk).update(name=first.name)
        assert sorted(Tenant.objects.values_list("pk", "name")) == [
            (first.pk, "Harbor Solar"),
            (second.pk, " harbor solar "),
            (renamed.pk, "Harbor Solar"),
        ]

        Tenant.objects.exclude(pk=first.pk).delete()
        _migrate(PREVIOUS)
        assert _name_index()
        assert Tenant.objects.get().name == "Harbor Solar"
    finally:
        _forward()
