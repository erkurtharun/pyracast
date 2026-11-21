from collections.abc import Callable
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from django.utils import timezone

from pyracast.accounts.models import (
    InvitationRevocation,
    OutboundMail,
    OutboundMailKind,
    OutboundMailStatus,
    WorkspaceInvitation,
)
from pyracast.accounts.retention import purge_closed_invitations
from pyracast.background.failures import ItemsFailedError
from pyracast.notifications.models import (
    DeliveryRecipient,
    NotificationDelivery,
    NotificationDeliveryStatus,
    PreparedMessageKind,
)
from pyracast.notifications.outbox import purge_finished_mail
from pyracast.notifications.retention import trim_rendered_mail
from pyracast.orchestration import dispatcher
from pyracast.retention import RETENTION, SWEEP_BATCH, Retention
from tests.support.domain import NOW, make_system
from tests.support.notifications import ready

pytestmark = pytest.mark.django_db(transaction=True)

CUTOFF = NOW - timedelta(days=90)
RENDERED = {"format": "plain", "subject": "Forecast unavailable", "body": "Not prepared."}


def test_the_retention_table_names_each_kind_of_row_with_its_period() -> None:
    assert {entry.rows: entry.period for entry in RETENTION} == {
        "sessions": timedelta(0),
        "abandoned_signups": timedelta(days=7),
        "finished_mail": timedelta(days=7),
        "closed_invitations": timedelta(days=90),
        "rendered_reminder_mail": timedelta(days=90),
    }
    assert SWEEP_BATCH == 500


def test_the_retention_phase_runs_every_sweep_with_its_cutoff_and_batch_despite_failed_items(
    monkeypatch: pytest.MonkeyPatch, pyracast_logs: pytest.LogCaptureFixture
) -> None:
    calls: list[tuple[str, datetime, int]] = []

    def sweep(rows: str, outcome: int | ItemsFailedError) -> Callable[[datetime, int], int]:
        def run(cutoff: datetime, limit: int) -> int:
            calls.append((rows, cutoff, limit))
            if isinstance(outcome, ItemsFailedError):
                raise outcome
            return outcome

        return run

    monkeypatch.setattr(timezone, "now", lambda: NOW)
    monkeypatch.setattr(dispatcher, "purge_expired", MagicMock())
    monkeypatch.setattr(dispatcher, "warn_before_purge", MagicMock())
    monkeypatch.setattr(
        dispatcher,
        "RETENTION",
        (
            Retention("failing", timedelta(days=2), sweep("failing", ItemsFailedError(2))),
            Retention("idle", timedelta(0), sweep("idle", 0)),
            Retention("busy", timedelta(days=90), sweep("busy", 3)),
        ),
    )

    with pytest.raises(ItemsFailedError) as failed:
        dispatcher._retention()

    assert failed.value.count == 2
    assert calls == [
        ("failing", NOW - timedelta(days=2), SWEEP_BATCH),
        ("idle", NOW, SWEEP_BATCH),
        ("busy", CUTOFF, SWEEP_BATCH),
    ]
    swept = [record.getMessage() for record in pyracast_logs.records]
    assert [line for line in swept if line.startswith("retention_swept")] == [
        "retention_swept rows=busy count=3"
    ]


def test_closed_invitations_leave_oldest_first_in_bounded_runs_once_their_period_ends() -> None:
    system = make_system()

    def invitation(name: str, **values: object) -> int:
        values.setdefault("expires_at", NOW + timedelta(days=7))
        return WorkspaceInvitation.objects.create(
            tenant=system.tenant, email=f"{name}@example.com", created_by=system.member, **values
        ).pk

    expired = invitation("expired", expires_at=CUTOFF - timedelta(seconds=3))
    revoked = invitation(
        "revoked",
        revoked_at=CUTOFF - timedelta(seconds=2),
        revoked_reason=InvitationRevocation.CANCELLED,
    )
    accepted = invitation(
        "accepted", accepted_at=CUTOFF - timedelta(seconds=1), accepted_by=system.admin
    )
    kept = {
        invitation("pending"),
        invitation("expired-at-cutoff", expires_at=CUTOFF),
        invitation("recent", accepted_at=CUTOFF + timedelta(seconds=1), accepted_by=system.admin),
    }
    OutboundMail.objects.create(
        kind=OutboundMailKind.WORKSPACE_INVITATION,
        recipient="expired@example.com",
        invitation_id=expired,
        language="en",
        created_at=CUTOFF,
        available_at=CUTOFF,
        expires_at=CUTOFF,
        status=OutboundMailStatus.SKIPPED,
        finished_at=NOW,
    )
    remaining = WorkspaceInvitation.objects.values_list("pk", flat=True)

    assert purge_closed_invitations(CUTOFF, 2) == 2
    assert set(remaining.all()) == kept | {accepted}
    assert not OutboundMail.objects.exists()
    assert purge_closed_invitations(CUTOFF, 2) == 1
    assert purge_closed_invitations(CUTOFF, 2) == 0
    assert set(remaining.all()) == kept
    assert not WorkspaceInvitation.objects.filter(pk__in=(expired, revoked)).exists()


def delivery_at(
    template: NotificationDelivery, scheduled_for: datetime, status: str, **values: object
) -> NotificationDelivery:
    delivery = NotificationDelivery.objects.create(
        schedule=template.schedule,
        binding=template.binding,
        scheduled_for=scheduled_for,
        preparation_deadline=scheduled_for,
        available_at=scheduled_for,
        status=status,
        prepared_message=RENDERED,
        forecast_kind=PreparedMessageKind.SAVED_PARTIAL,
        forecast_complete=False,
        prepared_at=scheduled_for,
        **values,
    )
    DeliveryRecipient.objects.create(
        delivery=delivery, address="a@example.com", key="a@example.com"
    )
    return delivery


def test_rendered_reminder_mail_of_ended_deliveries_is_trimmed_oldest_first_in_bounded_runs() -> (
    None
):
    template = ready()
    sent = delivery_at(template, CUTOFF - timedelta(hours=2), NotificationDeliveryStatus.SENT)
    failed = delivery_at(
        template,
        CUTOFF - timedelta(hours=1),
        NotificationDeliveryStatus.FAILED,
        last_error="mail_rejected",
    )
    uncertain = delivery_at(
        template, CUTOFF - timedelta(hours=3), NotificationDeliveryStatus.UNCERTAIN
    )
    recent = delivery_at(template, CUTOFF + timedelta(hours=1), NotificationDeliveryStatus.SENT)
    rendered = NotificationDelivery.objects.filter(prepared_message__isnull=False)

    assert trim_rendered_mail(CUTOFF, 1) == 1
    assert set(rendered.values_list("pk", flat=True)) == {failed.pk, uncertain.pk, recent.pk}
    assert trim_rendered_mail(CUTOFF, 1) == 1
    assert trim_rendered_mail(CUTOFF, 1) == 0
    assert set(rendered.values_list("pk", flat=True)) == {uncertain.pk, recent.pk}
    trimmed = NotificationDelivery.objects.get(pk=sent.pk)
    assert (
        trimmed.status,
        trimmed.forecast_kind,
        trimmed.forecast_complete,
        trimmed.prepared_at,
        trimmed.prepared_message,
    ) == ("sent", "saved_partial", False, sent.prepared_at, None)
    assert DeliveryRecipient.objects.filter(delivery=trimmed).count() == 1


def test_finished_mail_leaves_oldest_first_in_bounded_runs() -> None:
    def mail(finished_at: datetime | None, status: str) -> int:
        return OutboundMail.objects.create(
            kind=OutboundMailKind.PASSWORD_CHANGED,
            recipient="owner@example.com",
            language="en",
            created_at=NOW,
            available_at=NOW,
            expires_at=NOW,
            status=status,
            finished_at=finished_at,
        ).pk

    cutoff = NOW - timedelta(days=7)
    older = mail(cutoff - timedelta(seconds=2), OutboundMailStatus.SENT)
    old = mail(cutoff - timedelta(seconds=1), OutboundMailStatus.FAILED)
    kept = {mail(cutoff, OutboundMailStatus.SENT), mail(None, OutboundMailStatus.PENDING)}
    remaining = OutboundMail.objects.values_list("pk", flat=True)

    assert purge_finished_mail(cutoff, 1) == 1
    assert set(remaining.all()) == kept | {old}
    assert purge_finished_mail(cutoff, 1) == 1
    assert purge_finished_mail(cutoff, 1) == 0
    assert set(remaining.all()) == kept
    assert not OutboundMail.objects.filter(pk=older).exists()
