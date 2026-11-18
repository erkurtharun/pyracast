from collections.abc import Iterator
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from pyracast.accounts.models import Tenant, WorkspaceInvitation

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def leaf() -> Iterator[None]:
    targets = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        yield
    finally:
        MigrationExecutor(connection).migrate(targets)


def _columns() -> set[str]:
    with connection.cursor() as cursor:
        return {
            column.name
            for column in connection.introspection.get_table_description(
                cursor, "accounts_workspaceinvitation"
            )
        }


def _checks() -> set[str]:
    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(
            cursor, "accounts_workspaceinvitation"
        )
    return {name for name, details in constraints.items() if details["check"]}


@pytest.mark.usefixtures("leaf")
def test_invitation_role_migration_keeps_existing_invitations_as_viewer_invitations() -> None:
    tenant = Tenant.objects.create(name="Migrated workspace")
    sender = User.objects.create_user(username="sender")
    invitation = WorkspaceInvitation.objects.create(
        tenant=tenant,
        email="guest@example.com",
        token_hash="0" * 64,
        created_by=sender,
        expires_at=timezone.now() + timedelta(days=1),
    )
    MigrationExecutor(connection).migrate([("accounts", "0002_workspace_names_need_not_be_unique")])
    assert "role" not in _columns()

    MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())

    assert WorkspaceInvitation.objects.get(pk=invitation.pk).role == "viewer"
    assert "workspaceinvitation_role_chk" in _checks()
