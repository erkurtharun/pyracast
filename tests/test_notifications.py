from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from threading import Barrier
from typing import cast
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.models import User
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, close_old_connections, connection, connections, transaction
from rest_framework.test import APIClient

from pyracast.accounts.models import Membership, Tenant
from pyracast.accounts.operations import reassign_workspace_admin
from pyracast.admin_health_queries import notification_deliveries_for_issue
from pyracast.connections.models import ProviderAccount
from pyracast.forecasts.models import (
    CollectionJob,
    ForecastResult,
    RequestAttempt,
)
from pyracast.forecasts.monitoring import delivery_health
from pyracast.mail.errors import NotSentError, PermanentlyNotSentError
from pyracast.mail.send import send
from pyracast.notifications.jobs import (
    LEASE_DURATION,
    pending_notification_ids,
    process_notification,
)
from pyracast.notifications.models import (
    NotificationDelivery,
    NotificationSettings,
    ReminderSchedule,
)
from pyracast.notifications.operations import configure_reminders, get_reminder_schedule
from pyracast.notifications.schedule import local_slot
from pyracast.notifications.scheduler import enqueue_due
from pyracast.notifications.views import reminder_schedule_payload
from pyracast.regions.models import Region
from tests.support.domain import NOW, System, make_system
from tests.support.notifications import (
    SLOT,
    attached_sheet,
    cache_saved_days,
    configured,
    ready,
    saved,
)

pytestmark = pytest.mark.django_db(transaction=True)


def test_saved_delivery_pins_vintage_recipient_and_never_fetches() -> None:
    system, schedule = configured()
    snapshot = saved(system)
    requests, jobs = RequestAttempt.objects.count(), CollectionJob.objects.count()
    ids = enqueue_due(SLOT)
    assert enqueue_due(SLOT) == ids
    future = saved(system, SLOT + timedelta(minutes=2))
    assert process_notification(ids[0], SLOT + timedelta(minutes=2))
    assert not process_notification(ids[0], SLOT + timedelta(minutes=3))
    delivery = NotificationDelivery.objects.get(pk=ids[0])
    assert delivery.snapshot_id == snapshot.pk != future.pk
    assert delivery.recipients == ["default@example.com"]
    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert isinstance(message, EmailMultiAlternatives)
    assert message.bcc == ["default@example.com"]
    assert "Irradiance estimate, not plant electricity." in message.body
    assert "kWh/m²" in message.body
    assert "10 Nov 2026" in message.body
    assert "11 Nov 2026" in message.body
    assert "Partial:" in message.body
    sheet = attached_sheet(message)
    for day in (10, 11):
        row = next(cell.row for cell in sheet["A"] if cell.value == datetime(2026, 11, day))
        assert isinstance(sheet.cell(row, 3).value, float)
        assert "Partial estimate" in str(sheet.cell(row + 1, 1).value)
    assert len(message.alternatives) == len(message.attachments) == 1
    assert RequestAttempt.objects.count() == requests
    assert CollectionJob.objects.count() == jobs + 1
    payload = reminder_schedule_payload(schedule, SLOT)
    assert payload["quota"] == {"daily_reminder_limit": 10, "queries_per_email": 0}
    assert len(payload["deliveries"]) == 1  # type: ignore[arg-type]


@pytest.mark.parametrize("status", ["pending", "retry"])
def test_account_email_change_preserves_pinned_default_recipient(status: str) -> None:
    delivery = ready()
    NotificationDelivery.objects.filter(pk=delivery.pk).update(status=status)
    configured_by_id = delivery.schedule.configured_by_id
    assert configured_by_id is not None
    User.objects.filter(pk=configured_by_id).update(email="changed@example.com")
    assert process_notification(delivery.pk, SLOT)
    delivery.refresh_from_db()
    assert delivery.status == "sent"
    assert delivery.last_error == ""
    assert delivery.recipients == ["default@example.com"]
    assert mail.outbox[0].bcc == ["default@example.com"]


def test_default_email_change_preserves_explicit_region_recipient() -> None:
    system, _ = configured()
    configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        language="en",
        enabled=True,
        recipients=["region@example.com"],
        times=["10:00"],
        now=NOW,
    )
    saved(system)
    delivery_id = enqueue_due(SLOT)[0]
    User.objects.filter(pk=system.member.pk).update(email="changed@example.com")
    assert process_notification(delivery_id, SLOT)
    delivery = NotificationDelivery.objects.get(pk=delivery_id)
    assert delivery.status == "sent"
    assert delivery.recipients == ["region@example.com"]
    assert len(mail.outbox) == 1
    assert mail.outbox[0].bcc == ["region@example.com"]


@pytest.mark.parametrize(
    "times",
    [
        ["10:00", "10:00"],
        ["25:00"],
        ["09:61"],
        ["9:00"],
        ["aa:bb"],
        ["²0:00"],
        [],
        [f"{hour:02}:00" for hour in range(11)],
    ],
)
def test_configuration_validates_times(times: list[str]) -> None:
    system, _ = configured()
    with pytest.raises(ValidationError):
        configure_reminders(
            system.member,
            system.tenant.pk,
            system.region.pk,
            language="en",
            enabled=True,
            recipients=[],
            times=times,
            now=NOW,
        )


def test_configuration_authority_defaults_and_missing_email() -> None:
    system = make_system()
    empty = get_reminder_schedule(system.member, system.tenant.pk, system.region.pk, NOW)
    assert not empty.enabled
    assert reminder_schedule_payload(empty, NOW)["quota"] is None
    other = Tenant.objects.create(name="Other")
    with pytest.raises(PermissionDenied):
        get_reminder_schedule(system.member, other.pk, system.region.pk, NOW)
    Membership.objects.create(user=system.member, tenant=other)
    with pytest.raises(Region.DoesNotExist):
        get_reminder_schedule(system.member, other.pk, system.region.pk, NOW)
    with pytest.raises(ValidationError):
        configure_reminders(
            system.member,
            system.tenant.pk,
            system.region.pk,
            language="en",
            enabled=True,
            recipients=[],
            times=["10:00"],
            now=NOW,
        )
    with pytest.raises(ValidationError):
        configure_reminders(
            system.member,
            system.tenant.pk,
            system.region.pk,
            language="en",
            enabled=True,
            recipients=["override@example.com"],
            times=["10:00"],
            now=NOW,
        )
    with pytest.raises(ValidationError):
        configure_reminders(
            system.member,
            system.tenant.pk,
            system.region.pk,
            language="en",
            enabled=False,
            recipients=["bad"],
            times=[],
            now=NOW,
        )
    with pytest.raises(ValidationError):
        configure_reminders(
            system.member,
            system.tenant.pk,
            system.region.pk,
            language="en",
            enabled=False,
            recipients=[],
            times=[],
            now=NOW.replace(tzinfo=None),
        )


def test_override_and_no_saved_forecast_are_explicit() -> None:
    system, _ = configured()
    configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        language="en",
        enabled=True,
        recipients=["override@example.com"],
        times=["10:00"],
        now=NOW,
    )
    ids = enqueue_due(SLOT)
    delivery = NotificationDelivery.objects.get(pk=ids[0])
    assert delivery.status == "pending"
    assert delivery.recipients == ["override@example.com"]
    assert process_notification(delivery.pk, SLOT)
    assert "Tomorrow's forecast is unavailable" in mail.outbox[0].body
    saved(system)
    assert enqueue_due(SLOT) == ids
    assert not process_notification(delivery.pk, SLOT)
    assert not pending_notification_ids(SLOT)


def test_calendar_dst_gaps_folds_and_midnight() -> None:
    zone = ZoneInfo("America/New_York")
    assert local_slot(date(2026, 3, 8), "02:30", zone) is None
    assert local_slot(date(2026, 11, 1), "01:30", zone) == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert local_slot(date(2026, 11, 1), "00:00", zone) == datetime(2026, 11, 1, 4, tzinfo=UTC)
    with pytest.raises(ValidationError):
        enqueue_due(NOW.replace(tzinfo=None))
    with pytest.raises(ValidationError):
        process_notification(1, NOW.replace(tzinfo=None))


@pytest.mark.parametrize(
    "change",
    [
        "inactive",
        "membership",
        "suspended",
        "revision",
        "disabled",
        "schedule",
        "recipient",
        "binding",
        "wrong_region",
        "quota",
    ],
)
def test_worker_checks_resource_policy_independently_of_configurer(change: str) -> None:
    delivery = ready()
    schedule = delivery.schedule
    schedule.recipients = ["external@example.com"]
    schedule.recipient_memberships = {}
    schedule.save(update_fields=("recipients", "recipient_memberships"))
    delivery.recipients = ["external@example.com"]
    delivery.recipient_memberships = {}
    delivery.save(update_fields=("recipients", "recipient_memberships"))
    snapshot = delivery.snapshot
    assert snapshot is not None
    configured_by_id = schedule.configured_by_id
    assert configured_by_id is not None
    if change == "inactive":
        User.objects.filter(pk=configured_by_id).update(is_active=False)
    elif change == "membership":
        Membership.objects.filter(user_id=configured_by_id).delete()
    elif change == "suspended":
        Region.objects.filter(pk=schedule.region_id).update(status="suspended")
    elif change == "revision":
        Region.objects.filter(pk=schedule.region_id).update(revision=2)
    elif change == "disabled":
        ReminderSchedule.objects.filter(pk=schedule.pk).update(enabled=False)
    elif change == "schedule":
        ReminderSchedule.objects.filter(pk=schedule.pk).update(
            effective_from=SLOT + timedelta(seconds=1)
        )
    elif change == "recipient":
        NotificationDelivery.objects.filter(pk=delivery.pk).update(recipients=[""])
        ReminderSchedule.objects.filter(pk=schedule.pk).update(recipients=[""])
    elif change == "binding":
        Region.objects.filter(pk=schedule.region_id).update(status="pending", approved_binding=None)
    elif change == "wrong_region":
        region = Region.objects.create(
            tenant=schedule.region.tenant, name="Other", latitude=0, longitude=0, timezone="UTC"
        )
        ReminderSchedule.objects.filter(pk=schedule.pk).update(region=region)
    else:
        ProviderAccount.objects.filter(pk=snapshot.job.binding.provider_account_id).update(
            daily_quota=0
        )
    delivered = process_notification(delivery.pk, SLOT)
    assert delivered is (change in {"inactive", "membership", "schedule"})
    delivery.refresh_from_db()
    assert delivery.status == ("sent" if delivered else "skipped")
    if change == "recipient":
        assert delivery.last_error == "invalid_recipient"
    if change == "disabled":
        assert delivery.last_error == "cancelled_by_settings_change"


@pytest.mark.parametrize("mode", ["safe", "uncertain", "permanent"])
def test_delivery_failure_and_retry_policy(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    delivery = ready()
    monkeypatch.setattr("pyracast.notifications.jobs.secrets.randbelow", lambda bound: 0)

    def fail(message: EmailMultiAlternatives) -> None:
        assert not connection.in_atomic_block
        if mode == "safe":
            raise NotSentError
        if mode == "permanent":
            raise PermanentlyNotSentError
        raise TimeoutError

    assert not process_notification(delivery.pk, SLOT, sender=fail)
    delivery.refresh_from_db()
    assert (
        delivery.status == {"safe": "retry", "uncertain": "uncertain", "permanent": "failed"}[mode]
    )
    if mode == "safe":
        assert not process_notification(delivery.pk, SLOT + timedelta(seconds=30), sender=fail)
        assert pending_notification_ids(delivery.available_at) == [delivery.pk]
        assert not process_notification(delivery.pk, delivery.available_at, sender=fail)
        for attempt in range(3, 9):
            delivery.refresh_from_db()
            assert not process_notification(delivery.pk, delivery.available_at, sender=fail)
            delivery.refresh_from_db()
            assert delivery.attempts == attempt
        assert delivery.status == "failed"
        assert delivery.attempts == 8


def test_interrupted_transmission_never_retries_ambiguous_delivery() -> None:
    delivery = ready()
    NotificationDelivery.objects.filter(pk=delivery.pk).update(
        status="sending",
        prepared_message={"kind": "unavailable"},
        transmit_started_at=SLOT,
        lease_expires_at=SLOT + LEASE_DURATION,
    )
    assert not process_notification(delivery.pk, SLOT)
    assert not pending_notification_ids(SLOT)
    assert pending_notification_ids(SLOT + LEASE_DURATION) == [delivery.pk]
    assert not process_notification(delivery.pk, SLOT + LEASE_DURATION)
    delivery.refresh_from_db()
    assert delivery.status == "uncertain"
    NotificationDelivery.objects.filter(pk=delivery.pk).update(
        status="sending", lease_expires_at=None
    )
    assert pending_notification_ids(SLOT) == [delivery.pk]
    assert not process_notification(delivery.pk, SLOT)


def test_a_delivery_is_sending_only_with_prepared_content_in_a_known_state() -> None:
    delivery = ready()
    rows = NotificationDelivery.objects.filter(pk=delivery.pk)
    with (
        pytest.raises(IntegrityError, match="notification_delivery_status_valid"),
        transaction.atomic(),
    ):
        rows.update(status="unavailable")
    with (
        pytest.raises(IntegrityError, match="notification_delivery_sending_prepared"),
        transaction.atomic(),
    ):
        rows.update(status="sending", prepared_message=None)
    rows.update(status="sending", prepared_message={"kind": "unavailable"})
    assert rows.get().status == "sending"


@pytest.mark.parametrize("field", ["binding", "preparation_deadline"])
def test_a_delivery_always_pins_its_binding_and_preparation_deadline(field: str) -> None:
    rows = NotificationDelivery.objects.filter(pk=ready().pk)
    with (
        pytest.raises(IntegrityError, match="violates not-null constraint"),
        transaction.atomic(),
    ):
        rows.update(**{field: None})


def test_locmem_adapter_zero_return_is_known_not_sent() -> None:
    with pytest.raises(NotSentError):
        send(EmailMultiAlternatives(subject="Empty", body="No recipient", to=[]))


def test_full_summary_preserves_units_and_escapes_html() -> None:
    system, _ = configured()
    snapshot = saved(system)
    Region.objects.filter(pk=system.region.pk).update(name="<script>alert(1)</script>")
    start = datetime(2026, 11, 9, 21, tzinfo=UTC)
    snapshot.normalized_intervals = [
        {
            "start": (start + timedelta(hours=hour)).isoformat(),
            "end": (start + timedelta(hours=hour + 1)).isoformat(),
            "power_kw": 0.3,
            "sunlight": "day",
        }
        for hour in range(48)
    ]
    snapshot.issued_at = NOW - timedelta(hours=1)
    snapshot.save()
    cache_saved_days(snapshot)
    delivery_id = enqueue_due(SLOT)[0]
    assert process_notification(delivery_id, SLOT)
    message = mail.outbox[0]
    assert "kWh/m²" in message.body
    assert isinstance(message, EmailMultiAlternatives)
    summary = ForecastResult.objects.get(snapshot=snapshot).daily_payloads["2026-11-11"]["daylight"]
    sheet = attached_sheet(message)
    row = next(cell.row for cell in sheet["A"] if cell.value == datetime(2026, 11, 11))
    assert sheet.cell(row, 3).value == pytest.approx(summary["energy_kwh_m2"])
    html = message.alternatives[0][0]
    assert isinstance(html, str)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<table" in html


def test_stale_source_and_missed_schedule_are_visible() -> None:
    system, schedule = configured()
    saved(system, NOW - timedelta(days=2))
    ids = enqueue_due(SLOT)
    assert process_notification(ids[0], SLOT)
    assert "Forecast data is over 24 hours old." in mail.outbox[0].body
    next_slot = SLOT + timedelta(days=1)
    missed_ids = enqueue_due(next_slot + timedelta(hours=3))
    missed = NotificationDelivery.objects.get(pk=missed_ids[-1])
    assert missed.status == "skipped"
    assert missed.last_error == "missed_schedule"
    payload = reminder_schedule_payload(schedule, next_slot)
    assert isinstance(payload["deliveries"], list)
    assert payload["deliveries"][0]["stale"] is True


def test_worker_drops_old_pending_after_outage() -> None:
    delivery = ready()
    assert not process_notification(delivery.pk, SLOT + timedelta(hours=3))
    delivery.refresh_from_db()
    assert delivery.last_error == "missed_schedule"


@pytest.mark.parametrize("change", ["inactive", "unbound", "suspended", "membership"])
def test_scheduler_checks_binding_independently_of_configurer(change: str) -> None:
    system, schedule = configured()
    schedule.recipients = ["external@example.com"]
    schedule.recipient_memberships = {}
    schedule.save(update_fields=("recipients", "recipient_memberships"))
    saved(system)
    if change == "inactive":
        User.objects.filter(pk=system.member.pk).update(is_active=False)
    elif change == "unbound":
        Region.objects.filter(pk=system.region.pk).update(status="pending", approved_binding=None)
    elif change == "suspended":
        Region.objects.filter(pk=system.region.pk).update(status="suspended")
    else:
        Membership.objects.filter(user=system.member).delete()
    assert len(enqueue_due(SLOT)) == (1 if change in {"inactive", "membership"} else 0)


def test_scheduler_skips_dst_gap_and_future_slots() -> None:
    system, schedule = configured()
    system.region.timezone = "America/New_York"
    system.region.save()
    schedule.times = ["02:30", "23:00"]
    schedule.effective_from = datetime(2026, 3, 8, tzinfo=UTC)
    schedule.save()
    assert not enqueue_due(datetime(2026, 3, 8, 15, tzinfo=UTC))


def test_missing_snapshot_delivery_response_is_shared_and_read_only() -> None:
    system, schedule = configured()
    enqueue_due(SLOT)
    payload = reminder_schedule_payload(schedule, SLOT)
    assert isinstance(payload["deliveries"], list)
    assert payload["deliveries"][0]["collected_at"] is None
    user = User.objects.create(username="read_only")
    Membership.objects.create(user=user, tenant=system.tenant)
    before = ReminderSchedule.objects.count()
    shared = get_reminder_schedule(user, system.tenant.pk, system.region.pk, SLOT)
    assert shared.pk == schedule.pk
    assert reminder_schedule_payload(shared, SLOT) == payload
    assert ReminderSchedule.objects.count() == before


def test_disabling_shared_schedule_and_lowering_plan_cap() -> None:
    system, _ = configured()
    schedule = configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        language="en",
        enabled=False,
        recipients=[],
        times=[],
        now=SLOT,
    )
    assert not schedule.enabled
    assert not enqueue_due(SLOT)
    configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        language="en",
        enabled=True,
        recipients=[],
        times=["10:00"],
        now=SLOT,
    )
    ProviderAccount.objects.filter(pk=system.account.pk).update(daily_quota=0)
    assert not enqueue_due(SLOT)


def test_concurrent_schedulers_and_workers_are_idempotent() -> None:
    system, _ = configured()
    saved(system)
    barrier = Barrier(2)

    def enqueue() -> list[int]:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return enqueue_due(SLOT)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: enqueue(), range(2)))
    assert first == second
    assert NotificationDelivery.objects.count() == 1
    delivery = NotificationDelivery.objects.get()

    def process() -> bool:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return process_notification(delivery.pk, SLOT)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: process(), range(2)))
    assert sorted(results) == [False, True]


def test_api_shares_region_reads_and_restricts_writes_to_admin() -> None:
    system, _ = configured()
    other = User.objects.create(username="other")
    Membership.objects.create(user=other, tenant=system.tenant)
    client = APIClient()
    url = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/reminders/"
    assert client.get(url).status_code == 401
    client.force_authenticate(other)
    assert client.get(url).json()["enabled"]
    assert (
        client.put(
            url, {"enabled": False, "recipients": [], "times": [], "language": "en"}, format="json"
        ).status_code
        == 403
    )
    client.force_authenticate(system.member)
    response = client.put(
        url,
        {
            "enabled": True,
            "recipients": ["override@example.com"],
            "times": ["11:00"],
            "language": "en",
        },
        format="json",
    )
    assert response.status_code == 200
    assert response.json()["recipients"] == ["override@example.com"]
    assert (
        client.put(
            url,
            {"enabled": True, "recipients": [], "times": ["1:00"], "language": "en"},
            format="json",
        ).status_code
        == 400
    )
    other_tenant = Tenant.objects.create(name="Unrelated")
    assert (
        client.get(
            f"/api/tenants/{other_tenant.pk}/regions/{system.region.pk}/reminders/"
        ).status_code
        == 403
    )


def test_unsaved_reminder_schedule_defaults_to_request_language() -> None:
    system = make_system(approved=True)
    client = APIClient()
    client.force_authenticate(system.member)
    url = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/reminders/"
    assert client.get(url, HTTP_ACCEPT_LANGUAGE="tr").json()["language"] == "tr"
    assert client.get(url, HTTP_ACCEPT_LANGUAGE="en").json()["language"] == "en"
    assert not ReminderSchedule.objects.exists()


def test_recipient_api_accepts_five_and_admin_limit_changes_apply_immediately() -> None:
    system, _ = configured()
    client = APIClient()
    client.force_authenticate(system.member)
    url = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/reminders/"
    addresses = [f"person{index}@example.com" for index in range(6)]
    data = {"enabled": True, "recipients": addresses[:5], "times": ["10:00"], "language": "en"}
    response = client.put(url, data, format="json")
    assert response.status_code == 200
    assert response.json()["recipients"] == addresses[:5]
    assert response.json()["effective_recipients"] == addresses[:5]
    assert response.json()["recipient_limit"] == 5
    assert response.json()["recipient_limit_exceeded"] is False
    data["recipients"] = addresses
    assert client.put(url, data, format="json").status_code == 400
    NotificationSettings.objects.filter(pk=1).update(max_recipients=6)
    assert client.put(url, data, format="json").status_code == 200
    NotificationSettings.objects.filter(pk=1).update(max_recipients=2)
    response = client.get(url)
    assert response.json()["recipients"] == addresses
    assert response.json()["recipient_limit"] == 2
    assert response.json()["recipient_limit_exceeded"] is True


@pytest.mark.parametrize("recipients", [["bad"], [""], ["user@example.com", "USER@example.com"]])
def test_recipient_configuration_rejects_invalid_and_duplicate_addresses(
    recipients: list[str],
) -> None:
    system, _ = configured()
    with pytest.raises(ValidationError):
        configure_reminders(
            system.member,
            system.tenant.pk,
            system.region.pk,
            language="en",
            enabled=True,
            recipients=recipients,
            times=["10:00"],
            now=NOW,
        )


def test_recipients_trim_and_empty_input_snapshots_account_email() -> None:
    system, _ = configured()
    schedule = configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        language="en",
        enabled=True,
        recipients=[" person@example.com "],
        times=["10:00"],
        now=NOW,
    )
    assert schedule.recipients == ["person@example.com"]
    assert reminder_schedule_payload(schedule, NOW)["effective_recipients"] == [
        "person@example.com"
    ]
    schedule = configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        language="en",
        enabled=True,
        recipients=[],
        times=["10:00"],
        now=NOW,
    )
    assert reminder_schedule_payload(schedule, NOW)["effective_recipients"] == [
        "default@example.com"
    ]


def test_recipient_limit_reduction_blocks_whole_queued_list_without_truncation() -> None:
    system, schedule = configured()
    addresses = ["first@example.com", "second@example.com"]
    configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        language="en",
        enabled=True,
        recipients=addresses,
        times=["10:00"],
        now=NOW,
    )
    saved(system)
    ids = enqueue_due(SLOT)
    assert len(ids) == 1
    NotificationSettings.objects.filter(pk=1).update(max_recipients=1)
    enqueue_due(SLOT + timedelta(days=1))
    paused = NotificationDelivery.objects.get(scheduled_for=SLOT + timedelta(days=1))
    assert (paused.status, paused.last_error) == ("skipped", "recipient_limit_exceeded")
    assert paused.recipients == []
    assert all(not process_notification(delivery_id, SLOT) for delivery_id in ids)
    assert set(NotificationDelivery.objects.values_list("status", "last_error")) == {
        ("skipped", "recipient_limit_exceeded")
    }
    schedule.refresh_from_db()
    assert schedule.recipients == addresses
    assert not mail.outbox


def reconfigure(
    system: System,
    recipients: list[str],
    *,
    times: tuple[str, ...] = ("10:00",),
    enabled: bool = True,
    now: datetime = SLOT + timedelta(seconds=1),
) -> ReminderSchedule:
    return configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        language="en",
        enabled=enabled,
        recipients=recipients,
        times=list(times),
        now=now,
    )


def test_recipient_change_keeps_the_queued_row_for_still_permitted_recipients() -> None:
    system, _ = configured()
    reconfigure(system, ["a@example.com", "b@example.com"], now=NOW)
    saved(system)
    ids = enqueue_due(SLOT)
    reconfigure(system, ["b@example.com", "c@example.com"])
    assert process_notification(ids[0], SLOT + timedelta(seconds=2))
    assert mail.outbox[0].bcc == ["b@example.com"]
    assert NotificationDelivery.objects.get(pk=ids[0]).recipients == ["b@example.com"]


def test_unchanged_resave_keeps_the_queued_email() -> None:
    system, schedule = configured()
    saved(system)
    key = enqueue_due(SLOT)[0]
    resaved = reconfigure(system, [])
    assert resaved.pk == schedule.pk
    assert resaved.effective_from == NOW
    assert process_notification(key, SLOT + timedelta(seconds=2))
    assert mail.outbox[0].bcc == ["default@example.com"]


def test_unchanged_save_records_a_new_member_pin_and_keeps_a_due_slot() -> None:
    system, schedule = configured()
    reconfigure(system, ["joined@example.com"], now=NOW)
    saved(system)
    joined = User.objects.create(username="joined", email="joined@example.com")
    member = Membership.objects.create(user=joined, tenant=system.tenant)
    resaved = reconfigure(system, ["joined@example.com"])
    stored = ReminderSchedule.objects.get(pk=schedule.pk)
    assert (resaved.effective_from, stored.effective_from) == (NOW, NOW)
    assert stored.recipient_memberships == {"joined@example.com": [member.pk]}
    (key,) = enqueue_due(SLOT + timedelta(seconds=2))
    assert process_notification(key, SLOT + timedelta(seconds=3))
    assert mail.outbox[0].bcc == ["joined@example.com"]


def test_retry_row_survives_a_recipient_addition() -> None:
    system, _ = configured()
    reconfigure(system, ["a@example.com"], now=NOW)
    saved(system)
    key = enqueue_due(SLOT)[0]
    NotificationDelivery.objects.filter(pk=key).update(status="retry", attempts=1)
    reconfigure(system, ["a@example.com", "b@example.com"])
    assert process_notification(key, SLOT + timedelta(seconds=2))
    assert mail.outbox[0].bcc == ["a@example.com"]


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("limit", "recipient_limit_exceeded"),
        ("removed", "no_recipients"),
        ("malformed", "invalid_recipient"),
        ("invalid", "invalid_recipient"),
    ],
)
def test_queued_delivery_records_recipient_problem_found_by_the_worker(
    change: str, code: str
) -> None:
    system, schedule = configured()
    reconfigure(system, ["a@example.com", "b@example.com"], now=NOW)
    saved(system)
    key = enqueue_due(SLOT)[0]
    if change == "limit":
        NotificationSettings.objects.filter(pk=1).update(max_recipients=1)
    elif change == "removed":
        reconfigure(system, ["c@example.com"])
    elif change == "malformed":
        ReminderSchedule.objects.filter(pk=schedule.pk).update(recipients=[None])
    else:
        ReminderSchedule.objects.filter(pk=schedule.pk).update(
            recipients=["a@example.com", "not-an-address"]
        )
    assert not process_notification(key, SLOT + timedelta(seconds=2))
    delivery = NotificationDelivery.objects.get(pk=key)
    assert (delivery.status, delivery.last_error) == ("skipped", code)
    assert delivery_health(SLOT, timedelta(minutes=5))["paused_notifications"] == 1
    assert set(notification_deliveries_for_issue("paused_notifications", SLOT)) == {delivery}
    assert not mail.outbox


@pytest.mark.parametrize(
    "change",
    [
        {"enabled": False},
        {"recipients": ["b@example.com"]},
        {"include_workspace_members": True},
        {"times": ["11:00"]},
        {"language": "tr"},
    ],
)
def test_single_setting_change_is_stored_with_a_new_effective_date(
    change: dict[str, object],
) -> None:
    system, _ = configured()
    reconfigure(system, ["a@example.com"], now=NOW)
    arguments = {
        "language": "en",
        "enabled": True,
        "recipients": ["a@example.com"],
        "times": ["10:00"],
        "include_workspace_members": False,
    } | change
    later = NOW + timedelta(hours=1)
    schedule = configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=bool(arguments["enabled"]),
        recipients=cast(list[str], arguments["recipients"]),
        times=cast(list[str], arguments["times"]),
        language=str(arguments["language"]),
        include_workspace_members=bool(arguments["include_workspace_members"]),
        now=later,
    )
    stored = ReminderSchedule.objects.get(pk=schedule.pk)
    assert stored.effective_from == later
    for field, value in change.items():
        assert getattr(stored, field) == value


@pytest.mark.parametrize("status", ["pending", "retry"])
@pytest.mark.parametrize("change", ["time", "disabled"])
def test_removed_time_cancels_with_settings_change_code(change: str, status: str) -> None:
    system, _ = configured()
    saved(system)
    key = enqueue_due(SLOT)[0]
    NotificationDelivery.objects.filter(pk=key).update(status=status)
    if change == "time":
        reconfigure(system, [], times=("11:00",))
    else:
        reconfigure(system, [], enabled=False)
    assert not process_notification(key, SLOT + timedelta(seconds=2))
    delivery = NotificationDelivery.objects.get(pk=key)
    assert (delivery.status, delivery.last_error) == ("skipped", "cancelled_by_settings_change")
    assert not mail.outbox


@pytest.mark.parametrize("outcome", ["sent", "retry", "uncertain"])
def test_multiple_recipients_share_one_private_message_and_delivery_outcome(outcome: str) -> None:
    system, schedule = configured()
    addresses = ["ok@example.com", "retry@example.com", "unknown@example.com"]
    configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        language="en",
        enabled=True,
        recipients=addresses,
        times=["10:00"],
        now=NOW,
    )
    saved(system)
    (delivery_id,) = enqueue_due(SLOT)

    def sender(message: EmailMultiAlternatives) -> None:
        assert not message.to
        assert not message.cc
        assert message.bcc == addresses
        assert all(address not in message.body for address in addresses)
        if outcome == "retry":
            raise NotSentError
        if outcome == "uncertain":
            raise TimeoutError
        send(message)

    assert process_notification(delivery_id, SLOT, sender=sender) is (outcome == "sent")
    delivery = NotificationDelivery.objects.get(pk=delivery_id)
    assert delivery.recipients == addresses
    assert delivery.status == outcome
    assert pending_notification_ids(delivery.available_at) == (
        [delivery_id] if outcome == "retry" else []
    )
    assert len(mail.outbox) == (1 if outcome == "sent" else 0)
    payload = reminder_schedule_payload(schedule, SLOT)
    assert isinstance(payload["deliveries"], list)
    assert len(payload["deliveries"]) == 1
    assert payload["deliveries"][0]["recipients"] == addresses


def test_account_email_change_does_not_extend_already_pinned_slot() -> None:
    delivery = ready()
    assert process_notification(delivery.pk, SLOT)
    configured_by_id = delivery.schedule.configured_by_id
    assert configured_by_id is not None
    User.objects.filter(pk=configured_by_id).update(email="changed@example.com")
    assert enqueue_due(SLOT + timedelta(minutes=1)) == [delivery.pk]
    assert NotificationDelivery.objects.count() == 1
    assert len(mail.outbox) == 1


def test_fallback_configuration_reads_current_account_email_after_locking() -> None:
    system, _ = configured()
    User.objects.filter(pk=system.member.pk).update(email="fresh@example.com")
    schedule = configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        language="en",
        enabled=True,
        recipients=[],
        times=["10:00"],
        now=NOW,
    )
    assert reminder_schedule_payload(schedule, NOW)["effective_recipients"] == ["fresh@example.com"]
    User.objects.filter(pk=system.member.pk).update(email="")
    with pytest.raises(ValidationError):
        configure_reminders(
            system.member,
            system.tenant.pk,
            system.region.pk,
            language="en",
            enabled=True,
            recipients=[],
            times=["10:00"],
            now=NOW,
        )


def test_multiple_recipient_concurrent_dispatch_pins_one_grouped_delivery() -> None:
    system, _ = configured()
    configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        language="en",
        enabled=True,
        recipients=["a@example.com", "b@example.com"],
        times=["10:00"],
        now=NOW,
    )
    saved(system)
    barrier = Barrier(2)

    def enqueue() -> list[int]:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return enqueue_due(SLOT)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: enqueue(), range(2)))
    assert first == second
    assert len(first) == NotificationDelivery.objects.count() == 1
    delivery = NotificationDelivery.objects.get()
    assert delivery.recipients == ["a@example.com", "b@example.com"]


def test_notification_settings_database_constraints() -> None:
    NotificationSettings.objects.get_or_create(pk=1)
    with pytest.raises(IntegrityError), transaction.atomic():
        NotificationSettings.objects.filter(pk=1).update(max_recipients=0)
    with pytest.raises(IntegrityError), transaction.atomic():
        NotificationSettings.objects.create(pk=2)


def test_new_administrator_replaces_one_shared_recipient_list_within_the_cap() -> None:
    system, schedule = configured()
    addresses = [f"person{index}@example.com" for index in range(5)]
    configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        language="en",
        enabled=True,
        recipients=addresses,
        times=["10:00"],
        now=NOW,
    )
    other = User.objects.create(username="other", email="other@example.com")
    Membership.objects.create(user=other, tenant=system.tenant)
    reassign_workspace_admin(
        system.admin, system.tenant.pk, other.pk, expected_admin_id=system.member.pk
    )
    shared = get_reminder_schedule(other, system.tenant.pk, system.region.pk, NOW)
    assert shared.pk == schedule.pk
    assert shared.recipients == addresses
    with pytest.raises(ValidationError, match="in total"):
        configure_reminders(
            other,
            system.tenant.pk,
            system.region.pk,
            language="en",
            enabled=True,
            recipients=[*addresses, "extra@example.com"],
            times=["11:00"],
            now=NOW,
        )
    changed = configure_reminders(
        other,
        system.tenant.pk,
        system.region.pk,
        language="tr",
        enabled=True,
        recipients=["PERSON0@example.com"],
        times=["11:00"],
        now=NOW,
    )
    assert changed.pk == schedule.pk
    payload = reminder_schedule_payload(changed, NOW)
    assert payload["recipient_count"] == 1
    assert payload["recipient_slots_remaining"] == 4
    assert payload["recipients"] == ["PERSON0@example.com"]
    assert payload["language"] == "tr"
    assert "person1@example.com" not in str(payload)
    assert ReminderSchedule.objects.filter(region=system.region).count() == 1
    with pytest.raises(PermissionDenied):
        configure_reminders(
            system.member,
            system.tenant.pk,
            system.region.pk,
            language="en",
            enabled=True,
            recipients=addresses,
            times=["10:00"],
            now=NOW,
        )
    foreign = Tenant.objects.create(name="Foreign")
    with pytest.raises(PermissionDenied):
        configure_reminders(
            other,
            foreign.pk,
            system.region.pk,
            language="en",
            enabled=True,
            recipients=["intruder@example.com"],
            times=["10:00"],
            now=NOW,
        )
    schedule.refresh_from_db()
    assert schedule.recipients == ["PERSON0@example.com"]
    assert schedule.configured_by_id == other.pk


def test_paused_policy_preserves_recipients_and_empty_input_snapshots_account_email() -> None:
    system, _ = configured()
    schedule = configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        language="en",
        enabled=False,
        recipients=["saved@example.com"],
        times=[],
        now=NOW,
    )
    assert reminder_schedule_payload(schedule, NOW)["recipient_count"] == 1
    assert schedule.recipients == ["saved@example.com"]
    schedule = configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        language="en",
        enabled=False,
        recipients=[],
        times=[],
        now=NOW,
    )
    assert not schedule.enabled
    assert schedule.times == []
    assert schedule.recipients == ["default@example.com"]
    assert reminder_schedule_payload(schedule, NOW)["recipient_count"] == 1


def test_over_limit_list_allows_gradual_reduction_and_disabling_without_truncation() -> None:
    system, _ = configured()
    addresses = [f"person{index}@example.com" for index in range(5)]
    configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        language="en",
        enabled=True,
        recipients=addresses,
        times=["10:00"],
        now=NOW,
    )
    NotificationSettings.objects.filter(pk=1).update(max_recipients=2)
    paused = configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        language="en",
        enabled=False,
        recipients=addresses,
        times=["10:00"],
        now=NOW,
    )
    assert not paused.enabled
    assert paused.recipients == addresses
    assert reminder_schedule_payload(paused, NOW)["recipient_count"] == 5
    reduced = configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        language="en",
        enabled=True,
        recipients=addresses[:3],
        times=["10:00"],
        now=NOW,
    )
    assert reminder_schedule_payload(reduced, NOW)["recipient_limit_exceeded"] is True
    assert reminder_schedule_payload(reduced, NOW)["recipient_count"] == 3
    with pytest.raises(ValidationError):
        configure_reminders(
            system.member,
            system.tenant.pk,
            system.region.pk,
            language="en",
            enabled=True,
            recipients=addresses[:4],
            times=["10:00"],
            now=NOW,
        )
    recovered = configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        language="en",
        enabled=True,
        recipients=addresses[:2],
        times=["10:00"],
        now=NOW,
    )
    assert reminder_schedule_payload(recovered, NOW)["recipient_limit_exceeded"] is False
    assert recovered.recipients == addresses[:2]


@pytest.mark.parametrize("change", ["inactive", "membership"])
def test_configurer_inactivity_or_departure_keeps_the_shared_recipient_list(change: str) -> None:
    system, schedule = configured()
    schedule.recipients = ["external@example.com"]
    schedule.recipient_memberships = {}
    schedule.save(update_fields=("recipients", "recipient_memberships"))
    if change == "inactive":
        User.objects.filter(pk=system.member.pk).update(is_active=False)
    else:
        Membership.objects.filter(user=system.member).delete()
    assert reminder_schedule_payload(schedule, NOW)["recipient_count"] == 1
    assert reminder_schedule_payload(schedule, NOW)["recipients"] == ["external@example.com"]


def test_email_changes_do_not_expand_current_recipient_cap() -> None:
    system, current = configured()
    other = User.objects.create(username="other", email="default@example.com")
    Membership.objects.create(user=other, tenant=system.tenant)
    NotificationSettings.objects.filter(pk=1).update(max_recipients=1)
    saved(system)
    ids = enqueue_due(SLOT)
    assert len(ids) == 1
    User.objects.filter(pk=other.pk).update(email="changed@example.com")
    User.objects.filter(pk=system.member.pk).update(email="also-changed@example.com")
    later = enqueue_due(SLOT + timedelta(days=1))
    assert later
    assert NotificationDelivery.objects.filter(
        scheduled_for=SLOT + timedelta(days=1), schedule=current
    ).exists()
    assert all(process_notification(delivery_id, SLOT) for delivery_id in ids)
    assert len(mail.outbox) == 1
    assert mail.outbox[0].bcc == ["default@example.com"]
    assert reminder_schedule_payload(current, NOW)["recipient_count"] == 1
    assert set(NotificationDelivery.objects.values_list("schedule_id", flat=True)) == {current.pk}


def test_concurrent_viewer_cannot_change_shared_reminder_capacity() -> None:
    system, _ = configured()
    other = User.objects.create(username="other", email="default@example.com")
    Membership.objects.create(user=other, tenant=system.tenant)
    barrier = Barrier(2)

    def configure(user: User) -> bool:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                configure_reminders(
                    user,
                    system.tenant.pk,
                    system.region.pk,
                    language="en",
                    enabled=True,
                    recipients=[f"{user.username}{index}@example.com" for index in range(3)],
                    times=["10:00"],
                    now=NOW,
                )
            except PermissionDenied:
                return False
            return True
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(configure, [system.member, other]))
    assert outcomes == [True, False]
    assert not ReminderSchedule.objects.filter(configured_by=other).exists()


@pytest.mark.parametrize(
    "addresses", [[], ["one@example.com"], ["one@example.com", "two@example.com"]]
)
def test_reminder_api_exposes_only_canonical_recipient_fields(addresses: list[str]) -> None:
    system, schedule = configured()
    ReminderSchedule.objects.filter(pk=schedule.pk).update(recipients=addresses)
    client = APIClient()
    client.force_authenticate(system.member)
    response = client.get(f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/reminders/")
    assert response.status_code == 200
    payload = response.json()
    assert {"email", "effective_email"}.isdisjoint(payload)
    assert payload["recipients"] == addresses
    assert payload["effective_recipients"] == addresses


def test_reminder_api_requires_an_explicit_recipient_list() -> None:
    system, _ = configured()
    client = APIClient()
    client.force_authenticate(system.member)
    response = client.put(
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/reminders/",
        {"enabled": True, "times": ["10:00"], "language": "en"},
        format="json",
    )
    assert response.status_code == 400
    assert response.json() == {
        "code": "invalid_request",
        "message": "Check the highlighted values and try again.",
        "fields": {"recipients": "This field is required."},
    }
    response = client.put(
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/reminders/",
        {
            "enabled": True,
            "email": "obsolete@example.com",
            "recipients": [system.member.email],
            "times": ["10:00"],
            "language": "en",
        },
        format="json",
    )
    assert response.status_code == 400
    assert response.json() == {
        "code": "invalid_request",
        "message": "Unknown fields are not accepted.",
    }
