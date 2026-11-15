import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from pyracast.accounts.models import Membership, Tenant

pytestmark = pytest.mark.django_db(transaction=True)


def test_workspace_migration_preserves_records_and_resolves_name_collisions() -> None:
    before = [("accounts", "0004_userlanguagepreference")]
    after = [("accounts", "0005_workspaceadminchange_workspaceinvitation_and_more")]
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    executor.migrate(before)
    try:
        historical = executor.loader.project_state(before).apps
        old_tenant = historical.get_model("accounts", "Tenant")
        old_membership = historical.get_model("accounts", "Membership")
        old_user = historical.get_model("auth", "User")
        inactive = old_user.objects.create(username="inactive", is_active=False)
        first = old_user.objects.create(username="first", is_active=True)
        second = old_user.objects.create(username="second", is_active=True)
        original = old_tenant.objects.create(name="A" * 200)
        duplicate = old_tenant.objects.create(name="a" * 200)
        suffix = f" ({duplicate.pk})"
        reserved = old_tenant.objects.create(name="a" * (200 - len(suffix)) + suffix)
        empty = old_tenant.objects.create(name="\t ")
        solo = old_tenant.objects.create(name=" Unique name ")
        for user in (inactive, first, second):
            old_membership.objects.create(tenant=original, user=user)
        old_membership.objects.create(tenant=solo, user=inactive)
        original_ids = set(old_tenant.objects.values_list("pk", flat=True))
        executor = MigrationExecutor(connection)
        executor.migrate(after)
        assert set(Tenant.objects.values_list("pk", flat=True)) == original_ids
        assert Tenant.objects.get(pk=original.pk).name == "A" * 200
        assert Tenant.objects.get(pk=reserved.pk).name == reserved.name
        renamed = Tenant.objects.get(pk=duplicate.pk).name
        assert renamed.endswith(f" ({duplicate.pk}-1)")
        assert len(renamed) == 200
        assert Tenant.objects.get(pk=empty.pk).name == f"Workspace ({empty.pk})"
        assert Tenant.objects.get(pk=solo.pk).name == " Unique name "
        assert Membership.objects.count() == 4
        assert Membership.objects.get(tenant_id=original.pk, user_id=first.pk).role == "admin"
        assert Membership.objects.filter(role="admin").count() == 1
    finally:
        MigrationExecutor(connection).migrate(latest)
