from datetime import timedelta

import pytest
from django.db import IntegrityError, connection, models, transaction
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone
from psycopg.errors import CheckViolation

from pyracast.accounts.invitations import invite_workspace_member
from pyracast.accounts.models import OutboundMail, OutboundMailError, OutboundMailKind
from pyracast.regions.models import AuditAction, AuditEvent, Region
from tests.support.domain import make_system
from tests.support.migrations import forward, migrate

pytestmark = pytest.mark.django_db(transaction=True)

PREVIOUS = ("regions", "0007_audit_region_resubmitted")
# The leaves before this bundle's migrations: their audit and mail checks hold the other
# side's vocabulary.
EARLIER_LEAVES = (
    ("regions", "0006_reminder_schedule_audit"),
    ("notifications", "0007_reminder_recipient_rows"),
)


def test_every_region_is_kept_and_only_those_already_in_the_bin_are_never_warned() -> None:
    system = make_system()
    trashed_at = timezone.now() - timedelta(days=24)
    migrate(PREVIOUS)
    try:
        apps = MigrationExecutor(connection).loader.project_state([PREVIOUS]).apps
        regions = apps.get_model("regions", "Region")._default_manager
        for name, deleted_at in (("Live", None), ("Binned", trashed_at)):
            regions.create(
                tenant_id=system.tenant.pk,
                name=name,
                latitude=41 if deleted_at else 42,
                longitude=30,
                timezone="UTC",
                deleted_at=deleted_at,
            )
        before = sorted(regions.values_list("pk", "name", "status", "revision", "deleted_at"))
    finally:
        forward()

    rows = Region.objects.order_by("pk")
    assert sorted(rows.values_list("pk", "name", "status", "revision", "deleted_at")) == before
    marked = dict(rows.values_list("name", "purge_warning_sent_at"))
    assert marked["Live"] is None
    assert marked["Site"] is None
    assert marked["Binned"] is not None


def stores(row: models.Model, **values: object) -> bool:
    try:
        with transaction.atomic():
            type(row)._default_manager.filter(pk=row.pk).update(**values)
    except IntegrityError as error:
        if not isinstance(error.__cause__, CheckViolation):
            raise
        return False
    return True


def test_an_upgrade_from_the_earlier_leaves_admits_both_vocabularies_and_no_other_value() -> None:
    system = make_system()
    invite_workspace_member(system.member, system.tenant.pk, "guest@example.com")
    event = (
        AuditEvent.objects.filter(region=system.region)
        .exclude(action=AuditAction.REGION_CREATED)
        .earliest("pk")
    )
    mail = OutboundMail.objects.get()
    migrate(*EARLIER_LEAVES)
    try:
        before = (
            stores(event, action=AuditAction.REMINDER_SCHEDULE_UPDATED),
            stores(event, action=AuditAction.REGION_RESUBMITTED),
            stores(mail, kind=OutboundMailKind.PURGE_WARNING, invitation=None),
        )
    finally:
        forward()

    assert before == (True, False, False)
    assert all(stores(event, action=action) for action in AuditAction.values)
    assert all(
        stores(
            mail,
            kind=kind,
            invitation=mail.invitation if kind == OutboundMailKind.WORKSPACE_INVITATION else None,
        )
        for kind in OutboundMailKind.values
    )
    assert all(stores(mail, last_error=error) for error in OutboundMailError.values)
    assert not stores(event, action="xx")
    assert not stores(mail, kind="xx", invitation=None)
    assert not stores(mail, last_error="xx")
