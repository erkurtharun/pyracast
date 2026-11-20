from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError, connection, models, transaction
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from pyracast.accounts.models import Tenant, WorkspaceInvitation
from tests.support.migrations import checks, forward, migrate

pytestmark = pytest.mark.django_db(transaction=True)

PREVIOUS = ("accounts", "0007_invitation_token_minted_at_send")
LIFECYCLE = ("accounts", "0008_invitation_revocation_reason_and_acceptor")
STORED = (
    "tenant_id",
    "email",
    "token_hash",
    "created_by_id",
    "created_at",
    "expires_at",
    "accepted_at",
    "revoked_at",
    "delivery_status",
    "role",
)


@pytest.fixture
def at_previous_state() -> Iterator[None]:
    migrate(PREVIOUS)
    try:
        yield
    finally:
        forward()


def previous_invitations() -> models.Manager[models.Model]:
    state = MigrationExecutor(connection).loader.project_state(PREVIOUS)
    model: type[models.Model] = state.apps.get_model("accounts", "WorkspaceInvitation")
    return model._default_manager


@pytest.mark.usefixtures("at_previous_state")
def test_the_lifecycle_migration_keeps_every_invitation_and_records_no_reason_it_cannot_know() -> (
    None
):
    tenant = Tenant.objects.create(name="Migrated workspace")
    sender = User.objects.create_user(username="sender")
    invitee = User.objects.create_user(username="invitee")
    now = timezone.now()
    week = timedelta(days=7)
    invitations = previous_invitations()

    def stored(email: str, **values: Any) -> int:
        row = invitations.create(
            **{
                "tenant_id": tenant.pk,
                "created_by_id": sender.pk,
                "email": email,
                "token_hash": email.ljust(64, "0")[:64],
                "expires_at": now + week,
                **values,
            }
        )
        return int(row.pk)

    rows = {
        "pending": stored("pending@example.com"),
        "unsent": stored("unsent@example.com", token_hash=None),
        "administrator": stored("owner@example.com", role="admin"),
        "accepted": stored("accepted@example.com", accepted_at=now, delivery_status="sent"),
        "lapsed": stored("lapsed@example.com", expires_at=now - timedelta(days=1)),
        "revoked": stored("revoked@example.com", expires_at=now, revoked_at=now),
        "failed": stored("failed@example.com", delivery_status="failed"),
    }
    before = {pk: values for pk, *values in invitations.order_by("pk").values_list("pk", *STORED)}

    migrate(LIFECYCLE)

    after = WorkspaceInvitation.objects.order_by("pk")
    assert {pk: values for pk, *values in after.values_list("pk", *STORED)} == before
    assert set(after.values_list("revoked_reason", flat=True)) == {""}
    assert not after.filter(accepted_by__isnull=False).exists()
    assert {
        name: WorkspaceInvitation.objects.get(pk=pk).state_at(timezone.now())
        for name, pk in rows.items()
    } == {
        "pending": "pending",
        "unsent": "pending",
        "administrator": "pending",
        "accepted": "accepted",
        "lapsed": "expired",
        "revoked": "revoked",
        "failed": "pending",
    }
    assert {
        "workspaceinvitation_revoked_at_chk",
        "workspaceinvitation_revoked_reason_chk",
        "workspaceinvitation_accepted_by_chk",
    } <= checks("accounts_workspaceinvitation")

    pending = WorkspaceInvitation.objects.filter(pk=rows["pending"])
    accepted = WorkspaceInvitation.objects.filter(pk=rows["accepted"])
    refusals: tuple[Callable[[], int], ...] = (
        lambda: pending.update(revoked_reason="cancelled"),
        lambda: pending.update(revoked_reason="unknown", revoked_at=now),
        lambda: pending.update(accepted_by_id=invitee.pk),
        lambda: accepted.update(revoked_at=now, revoked_reason="cancelled"),
    )
    for refused in refusals:
        with pytest.raises(IntegrityError), transaction.atomic():
            refused()
    pending.update(revoked_at=now, revoked_reason="cancelled")
    accepted.update(accepted_by_id=invitee.pk)
    assert pending.get().expires_at == now + week
