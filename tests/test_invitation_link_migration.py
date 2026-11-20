from collections.abc import Iterator
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from pyracast.accounts.models import Tenant, WorkspaceInvitation
from tests.support.migrations import forward, migrate

pytestmark = pytest.mark.django_db(transaction=True)

PREVIOUS = ("accounts", "0005_schema_naming_convention")
EXPIRING = ("accounts", "0006_expire_invitations_with_the_previous_link")
MINTED_AT_SEND = ("accounts", "0007_invitation_token_minted_at_send")


@pytest.fixture
def at_previous_state() -> Iterator[None]:
    migrate(PREVIOUS)
    try:
        yield
    finally:
        forward()


def _invitation(
    tenant: Tenant, sender: User, email: str, expires_in: timedelta, **fields: object
) -> WorkspaceInvitation:
    return WorkspaceInvitation.objects.create(
        tenant=tenant,
        email=email,
        token_hash=email.ljust(64, "0")[:64],
        created_by=sender,
        expires_at=timezone.now() + expires_in,
        **fields,
    )


@pytest.mark.usefixtures("at_previous_state")
def test_the_link_migration_expires_only_the_invitations_still_open() -> None:
    tenant = Tenant.objects.create(name="Linked workspace")
    sender = User.objects.create_user(username="sender")
    now = timezone.now()
    open_viewer = _invitation(tenant, sender, "open@example.com", timedelta(days=6))
    open_admin = _invitation(tenant, sender, "admin@example.com", timedelta(days=1), role="admin")
    accepted = _invitation(
        tenant, sender, "accepted@example.com", timedelta(days=5), accepted_at=now
    )
    revoked = _invitation(
        tenant, sender, "revoked@example.com", timedelta(0), revoked_at=now + timedelta(seconds=1)
    )
    lapsed = _invitation(tenant, sender, "lapsed@example.com", timedelta(days=-2))
    untouched = {
        row.pk: (row.expires_at, row.accepted_at, row.revoked_at)
        for row in (accepted, revoked, lapsed)
    }

    migrate(EXPIRING)

    after = timezone.now()
    for row in (open_viewer, open_admin):
        expired = WorkspaceInvitation.objects.get(pk=row.pk)
        assert now <= expired.expires_at <= after
        assert expired.accepted_at is None
        assert expired.revoked_at is None
    for pk, fields in untouched.items():
        row = WorkspaceInvitation.objects.get(pk=pk)
        assert (row.expires_at, row.accepted_at, row.revoked_at) == fields
    assert not WorkspaceInvitation.objects.filter(
        accepted_at__isnull=True, revoked_at__isnull=True, expires_at__gt=after
    ).exists()


@pytest.mark.usefixtures("at_previous_state")
def test_the_link_expiry_runs_before_the_token_is_minted_at_send_and_keeps_every_hash() -> None:
    tenant = Tenant.objects.create(name="Linked workspace")
    sender = User.objects.create_user(username="sender")
    now = timezone.now()
    open_row = _invitation(tenant, sender, "open@example.com", timedelta(days=6))
    accepted = _invitation(
        tenant, sender, "accepted@example.com", timedelta(days=5), accepted_at=now
    )
    hashes = dict(WorkspaceInvitation.objects.values_list("pk", "token_hash"))
    plan = MigrationExecutor(connection).migration_plan([MINTED_AT_SEND])

    assert [
        (migration.app_label, migration.name)
        for migration, _ in plan
        if migration.app_label == "accounts"
    ] == [EXPIRING, MINTED_AT_SEND]

    migrate(MINTED_AT_SEND)

    assert WorkspaceInvitation.objects.get(pk=open_row.pk).expires_at <= timezone.now()
    assert WorkspaceInvitation.objects.get(pk=accepted.pk).expires_at == accepted.expires_at
    assert dict(WorkspaceInvitation.objects.values_list("pk", "token_hash")) == hashes
    unsent = WorkspaceInvitation.objects.create(
        tenant=tenant, email="unsent@example.com", created_by=sender, expires_at=now
    )
    assert WorkspaceInvitation.objects.get(pk=unsent.pk).token_hash is None
