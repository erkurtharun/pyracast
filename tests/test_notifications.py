from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from threading import Barrier
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.models import User
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, close_old_connections, connection, connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings
from rest_framework.test import APIClient

from pyracast.accounts.models import Membership, Tenant
from pyracast.connections.models import ProviderAccount
from pyracast.forecasts.models import (
    CollectionJob,
    ForecastResult,
    ForecastSnapshot,
    RequestAttempt,
)
from pyracast.notifications.jobs import (
    LEASE_DURATION,
    pending_notification_ids,
    process_notification,
)
from pyracast.notifications.mail import NotSentError, send_message
from pyracast.notifications.models import (
    NotificationDelivery,
    NotificationSettings,
    ReminderPreference,
)
from pyracast.notifications.operations import configure_reminders, get_preference
from pyracast.notifications.schedule import local_slot
from pyracast.notifications.scheduler import enqueue_due
from pyracast.notifications.views import preference_payload
from pyracast.regions.models import Region
from tests.test_domain import NOW, System, make_system

pytestmark = pytest.mark.django_db(transaction=True)
SLOT = NOW + timedelta(days=1) - timedelta(hours=6)


@pytest.fixture(autouse=True)
def memory_mail() -> Iterator[None]:
    with override_settings(
        MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
    ):
        yield


def configured() -> tuple[System, ReminderPreference]:
    system = make_system(approved=True)
    system.member.email = "default@example.com"
    system.member.save()
    preference = configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        recipients=[],
        times=["10:00"],
        now=NOW,
    )
    return system, preference


def saved(system: System, when: datetime = NOW) -> ForecastSnapshot:
    job = CollectionJob.objects.create(
        binding=system.binding,
        scheduled_slot=when - timedelta(minutes=1),
        target_date=when.date() + timedelta(days=1),
        available_at=when,
        status="succeeded",
    )
    snapshot = ForecastSnapshot.objects.create(
        job=job,
        binding=system.binding,
        raw_payload={},
        normalized_intervals=[
            {
                "start": when.isoformat(),
                "end": (when + timedelta(minutes=30)).isoformat(),
                "power_kw": 0.3,
                "sunlight": "day",
            },
        ],
        collected_at=when,
        source_kind="forecast",
    )
    ForecastResult.objects.create(snapshot=snapshot, payload={}, published_at=when)
    return snapshot


def ready() -> NotificationDelivery:
    system, _ = configured()
    saved(system)
    ids = enqueue_due(SLOT)
    assert len(ids) == 1
    return NotificationDelivery.objects.get(pk=ids[0])


def test_saved_delivery_pins_vintage_recipient_and_never_fetches() -> None:
    system, preference = configured()
    snapshot = saved(system)
    requests, jobs = RequestAttempt.objects.count(), CollectionJob.objects.count()
    ids = enqueue_due(SLOT)
    assert enqueue_due(SLOT) == ids
    future = saved(system, SLOT + timedelta(minutes=1))
    assert process_notification(ids[0], SLOT + timedelta(minutes=2))
    assert not process_notification(ids[0], SLOT + timedelta(minutes=3))
    delivery = NotificationDelivery.objects.get(pk=ids[0])
    assert delivery.snapshot_id == snapshot.pk != future.pk
    assert delivery.recipient == "default@example.com"
    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert isinstance(message, EmailMultiAlternatives)
    assert message.to == ["default@example.com"]
    assert "Unvalidated" in message.body
    assert "kWh/m²" in message.body
    assert "2026-11-10" in message.body
    assert "2026-11-11" in message.body
    assert "Unavailable (incomplete coverage)" in message.body
    assert "No new provider request" in message.body
    assert len(message.alternatives) == len(message.attachments) == 1
    assert RequestAttempt.objects.count() == requests
    assert CollectionJob.objects.count() == jobs + 1
    payload = preference_payload(preference, SLOT)
    assert payload["quota"] == {"daily_reminder_limit": 10, "queries_per_email": 0}
    assert len(payload["deliveries"]) == 1  # type: ignore[arg-type]


@pytest.mark.parametrize("status", ["pending", "retry"])
def test_default_email_change_skips_pinned_delivery(status: str) -> None:
    delivery = ready()
    NotificationDelivery.objects.filter(pk=delivery.pk).update(status=status)
    User.objects.filter(pk=delivery.preference.user_id).update(email="changed@example.com")
    assert not process_notification(delivery.pk, SLOT)
    delivery.refresh_from_db()
    assert delivery.status == "skipped"
    assert delivery.last_error == "preference_or_authority_changed"
    assert delivery.recipient == "default@example.com"
    assert not mail.outbox


def test_default_email_change_preserves_explicit_region_recipient() -> None:
    system, _ = configured()
    configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
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
    assert delivery.recipient == "region@example.com"
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["region@example.com"]


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
            enabled=True,
            recipients=[],
            times=times,
            now=NOW,
        )


def test_configuration_authority_defaults_and_missing_email() -> None:
    system = make_system()
    empty = get_preference(system.member, system.tenant.pk, system.region.pk, NOW)
    assert not empty.enabled
    assert preference_payload(empty, NOW)["quota"] is None
    other = Tenant.objects.create(name="Other")
    with pytest.raises(PermissionDenied):
        get_preference(system.member, other.pk, system.region.pk, NOW)
    Membership.objects.create(user=system.member, tenant=other)
    with pytest.raises(Region.DoesNotExist):
        get_preference(system.member, other.pk, system.region.pk, NOW)
    with pytest.raises(ValidationError):
        configure_reminders(
            system.member,
            system.tenant.pk,
            system.region.pk,
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
        enabled=True,
        recipients=["override@example.com"],
        times=["10:00"],
        now=NOW,
    )
    ids = enqueue_due(SLOT)
    delivery = NotificationDelivery.objects.get(pk=ids[0])
    assert delivery.status == "unavailable"
    assert delivery.last_error == "no_saved_forecast"
    assert delivery.recipient == "override@example.com"
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
        "preference",
        "recipient",
        "no_snapshot",
        "wrong_region",
        "source_kind",
        "future_snapshot",
        "future_publish",
        "quota",
    ],
)
def test_worker_rechecks_authority_and_snapshot(change: str) -> None:
    delivery = ready()
    preference = delivery.preference
    snapshot = delivery.snapshot
    assert snapshot is not None
    if change == "inactive":
        User.objects.filter(pk=preference.user_id).update(is_active=False)
    elif change == "membership":
        Membership.objects.filter(user_id=preference.user_id).delete()
    elif change == "suspended":
        Region.objects.filter(pk=preference.region_id).update(status="suspended")
    elif change == "revision":
        Region.objects.filter(pk=preference.region_id).update(revision=2)
    elif change == "disabled":
        ReminderPreference.objects.filter(pk=preference.pk).update(enabled=False)
    elif change == "preference":
        ReminderPreference.objects.filter(pk=preference.pk).update(
            effective_from=SLOT + timedelta(seconds=1)
        )
    elif change == "recipient":
        NotificationDelivery.objects.filter(pk=delivery.pk).update(recipient="")
        User.objects.filter(pk=preference.user_id).update(email="")
    elif change == "no_snapshot":
        NotificationDelivery.objects.filter(pk=delivery.pk).update(snapshot=None)
    elif change == "wrong_region":
        region = Region.objects.create(
            tenant=preference.region.tenant, name="Other", latitude=0, longitude=0, timezone="UTC"
        )
        ReminderPreference.objects.filter(pk=preference.pk).update(region=region)
    elif change == "source_kind":
        ForecastSnapshot.objects.filter(pk=snapshot.pk).update(source_kind="estimated_actuals")
    elif change == "future_snapshot":
        ForecastSnapshot.objects.filter(pk=snapshot.pk).update(
            collected_at=SLOT + timedelta(seconds=1)
        )
    elif change == "future_publish":
        ForecastResult.objects.filter(snapshot=snapshot).update(
            published_at=SLOT + timedelta(seconds=1)
        )
    else:
        ProviderAccount.objects.filter(pk=snapshot.job.binding.provider_account_id).update(
            daily_quota=0
        )
    assert not process_notification(delivery.pk, SLOT)
    delivery.refresh_from_db()
    assert delivery.status == "skipped"


@pytest.mark.parametrize("mode", ["safe", "uncertain", "calculation"])
def test_delivery_failure_and_retry_policy(mode: str) -> None:
    delivery = ready()
    if mode == "calculation":
        assert delivery.snapshot is not None
        ForecastSnapshot.objects.filter(pk=delivery.snapshot.pk).update(
            normalized_intervals=[{"start": 7}]
        )
        assert not process_notification(delivery.pk, SLOT)
        delivery.refresh_from_db()
        assert (delivery.status, delivery.last_error) == ("unavailable", "calculation_failure")
        return

    def fail(message: EmailMultiAlternatives) -> None:
        assert not connection.in_atomic_block
        if mode == "safe":
            raise NotSentError
        raise TimeoutError

    assert not process_notification(delivery.pk, SLOT, sender=fail)
    delivery.refresh_from_db()
    assert delivery.status == ("retry" if mode == "safe" else "uncertain")
    if mode == "safe":
        assert not process_notification(delivery.pk, SLOT + timedelta(seconds=30), sender=fail)
        assert pending_notification_ids(SLOT + timedelta(seconds=60)) == [delivery.pk]
        assert not process_notification(delivery.pk, SLOT + timedelta(seconds=60), sender=fail)
        assert not process_notification(delivery.pk, SLOT + timedelta(seconds=120), sender=fail)
        delivery.refresh_from_db()
        assert delivery.status == "failed"
        assert delivery.attempts == 3


def test_interrupted_sending_never_retries_ambiguous_delivery() -> None:
    delivery = ready()
    NotificationDelivery.objects.filter(pk=delivery.pk).update(
        status="sending", lease_expires_at=SLOT + LEASE_DURATION
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


def test_locmem_adapter_zero_return_is_known_not_sent() -> None:
    with pytest.raises(NotSentError):
        send_message(EmailMultiAlternatives(subject="Empty", body="No recipient", to=[]))


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
    delivery_id = enqueue_due(SLOT)[0]
    assert process_notification(delivery_id, SLOT)
    message = mail.outbox[0]
    assert "7.200 kWh/m²" in message.body
    assert isinstance(message, EmailMultiAlternatives)
    html = message.alternatives[0][0]
    assert isinstance(html, str)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<table" in html


def test_stale_source_and_missed_schedule_are_visible() -> None:
    system, preference = configured()
    saved(system, NOW - timedelta(days=2))
    ids = enqueue_due(SLOT)
    assert process_notification(ids[0], SLOT)
    assert "Stale saved forecast" in mail.outbox[0].body
    next_slot = SLOT + timedelta(days=1)
    missed_ids = enqueue_due(next_slot + timedelta(hours=3))
    missed = NotificationDelivery.objects.get(pk=missed_ids[-1])
    assert missed.status == "skipped"
    assert missed.last_error == "missed_schedule"
    payload = preference_payload(preference, next_slot)
    assert isinstance(payload["deliveries"], list)
    assert payload["deliveries"][0]["stale"] is True


def test_worker_drops_old_pending_after_outage() -> None:
    delivery = ready()
    assert not process_notification(delivery.pk, SLOT + timedelta(hours=3))
    delivery.refresh_from_db()
    assert delivery.last_error == "missed_schedule"


@pytest.mark.parametrize("change", ["inactive", "unbound", "suspended", "membership"])
def test_scheduler_rechecks_authority(change: str) -> None:
    system, _ = configured()
    saved(system)
    if change == "inactive":
        User.objects.filter(pk=system.member.pk).update(is_active=False)
    elif change == "unbound":
        Region.objects.filter(pk=system.region.pk).update(approved_binding=None)
    elif change == "suspended":
        Region.objects.filter(pk=system.region.pk).update(status="suspended")
    else:
        Membership.objects.filter(user=system.member).delete()
    assert not enqueue_due(SLOT)


def test_scheduler_skips_dst_gap_and_future_slots() -> None:
    system, preference = configured()
    system.region.timezone = "America/New_York"
    system.region.save()
    preference.times = ["02:30", "23:00"]
    preference.effective_from = datetime(2026, 3, 8, tzinfo=UTC)
    preference.save()
    assert not enqueue_due(datetime(2026, 3, 8, 15, tzinfo=UTC))


def test_missing_snapshot_delivery_response_and_read_only_default() -> None:
    system, preference = configured()
    enqueue_due(SLOT)
    payload = preference_payload(preference, SLOT)
    assert isinstance(payload["deliveries"], list)
    assert payload["deliveries"][0]["collected_at"] is None
    user = User.objects.create(username="read_only")
    Membership.objects.create(user=user, tenant=system.tenant)
    before = ReminderPreference.objects.count()
    empty = get_preference(user, system.tenant.pk, system.region.pk, SLOT)
    assert preference_payload(empty, SLOT)["deliveries"] == []
    assert ReminderPreference.objects.count() == before


def test_disabling_preferences_and_lowering_plan_cap() -> None:
    system, _ = configured()
    preference = configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=False,
        recipients=[],
        times=[],
        now=SLOT,
    )
    assert not preference.enabled
    assert not enqueue_due(SLOT)
    configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
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


def test_api_is_per_user_and_tenant_scoped() -> None:
    system, _ = configured()
    other = User.objects.create(username="other")
    Membership.objects.create(user=other, tenant=system.tenant)
    client = APIClient()
    url = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/reminders/"
    assert client.get(url).status_code == 403
    client.force_authenticate(other)
    assert not client.get(url).json()["enabled"]
    client.force_authenticate(system.member)
    response = client.put(
        url,
        {"enabled": True, "recipients": ["override@example.com"], "times": ["11:00"]},
        format="json",
    )
    assert response.status_code == 200
    assert response.json()["recipients"] == ["override@example.com"]
    assert (
        client.put(
            url, {"enabled": True, "recipients": [], "times": ["1:00"]}, format="json"
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


def test_recipient_api_accepts_five_and_admin_limit_changes_apply_immediately() -> None:
    system, _ = configured()
    client = APIClient()
    client.force_authenticate(system.member)
    url = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/reminders/"
    addresses = [f"person{index}@example.com" for index in range(6)]
    data = {"enabled": True, "recipients": addresses[:5], "times": ["10:00"]}
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
            enabled=True,
            recipients=recipients,
            times=["10:00"],
            now=NOW,
        )


def test_recipients_trim_and_empty_list_uses_account_email() -> None:
    system, _ = configured()
    preference = configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        recipients=[" person@example.com "],
        times=["10:00"],
        now=NOW,
    )
    assert preference.recipients == ["person@example.com"]
    assert preference_payload(preference, NOW)["effective_recipients"] == ["person@example.com"]
    preference = configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        recipients=[],
        times=["10:00"],
        now=NOW,
    )
    assert preference_payload(preference, NOW)["effective_recipients"] == ["default@example.com"]


def test_recipient_limit_reduction_blocks_whole_queued_list_without_truncation() -> None:
    system, preference = configured()
    addresses = ["first@example.com", "second@example.com"]
    configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        recipients=addresses,
        times=["10:00"],
        now=NOW,
    )
    saved(system)
    ids = enqueue_due(SLOT)
    assert len(ids) == 2
    NotificationSettings.objects.filter(pk=1).update(max_recipients=1)
    assert not enqueue_due(SLOT + timedelta(days=1))
    assert all(not process_notification(delivery_id, SLOT) for delivery_id in ids)
    assert set(NotificationDelivery.objects.values_list("status", flat=True)) == {"skipped"}
    preference.refresh_from_db()
    assert preference.recipients == addresses
    assert not mail.outbox


def test_recipient_change_cancels_every_old_pending_delivery() -> None:
    system, _ = configured()
    configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        recipients=["a@example.com", "b@example.com"],
        times=["10:00"],
        now=NOW,
    )
    saved(system)
    ids = enqueue_due(SLOT)
    configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        recipients=["b@example.com", "c@example.com"],
        times=["10:00"],
        now=SLOT + timedelta(seconds=1),
    )
    assert all(
        not process_notification(delivery_id, SLOT + timedelta(seconds=2)) for delivery_id in ids
    )
    assert set(NotificationDelivery.objects.values_list("status", flat=True)) == {"skipped"}
    assert not mail.outbox


def test_multiple_recipient_outcomes_are_independent_and_private() -> None:
    system, preference = configured()
    configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        recipients=["ok@example.com", "retry@example.com", "unknown@example.com"],
        times=["10:00"],
        now=NOW,
    )
    saved(system)
    ids = enqueue_due(SLOT)

    def sender(message: EmailMultiAlternatives) -> None:
        assert len(message.to) == 1
        assert not message.cc
        assert not message.bcc
        if message.to == ["retry@example.com"]:
            raise NotSentError
        if message.to == ["unknown@example.com"]:
            raise TimeoutError
        send_message(message)

    assert [process_notification(delivery_id, SLOT, sender=sender) for delivery_id in ids] == [
        True,
        False,
        False,
    ]
    assert dict(NotificationDelivery.objects.values_list("recipient", "status")) == {
        "ok@example.com": "sent",
        "retry@example.com": "retry",
        "unknown@example.com": "uncertain",
    }
    assert pending_notification_ids(SLOT + timedelta(minutes=1)) == [ids[1]]
    assert len(mail.outbox) == 1
    payload = preference_payload(preference, SLOT)
    assert isinstance(payload["deliveries"], list)
    assert {row["recipient"] for row in payload["deliveries"]} == {
        "ok@example.com",
        "retry@example.com",
        "unknown@example.com",
    }


def test_account_email_change_does_not_extend_already_pinned_slot() -> None:
    delivery = ready()
    assert process_notification(delivery.pk, SLOT)
    User.objects.filter(pk=delivery.preference.user_id).update(email="changed@example.com")
    assert enqueue_due(SLOT + timedelta(minutes=1)) == [delivery.pk]
    assert NotificationDelivery.objects.count() == 1
    assert len(mail.outbox) == 1


def test_fallback_configuration_reads_current_account_email_after_locking() -> None:
    system, _ = configured()
    User.objects.filter(pk=system.member.pk).update(email="fresh@example.com")
    preference = configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        recipients=[],
        times=["10:00"],
        now=NOW,
    )
    assert preference_payload(preference, NOW)["effective_recipients"] == ["fresh@example.com"]
    User.objects.filter(pk=system.member.pk).update(email="")
    with pytest.raises(ValidationError):
        configure_reminders(
            system.member,
            system.tenant.pk,
            system.region.pk,
            enabled=True,
            recipients=[],
            times=["10:00"],
            now=NOW,
        )


def test_multiple_recipient_concurrent_dispatch_pins_one_delivery_per_address() -> None:
    system, _ = configured()
    configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
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
    assert len(first) == NotificationDelivery.objects.count() == 2
    assert set(NotificationDelivery.objects.values_list("recipient", flat=True)) == {
        "a@example.com",
        "b@example.com",
    }


def test_notification_settings_database_constraints() -> None:
    NotificationSettings.objects.get_or_create(pk=1)
    with pytest.raises(IntegrityError), transaction.atomic():
        NotificationSettings.objects.filter(pk=1).update(max_recipients=0)
    with pytest.raises(IntegrityError), transaction.atomic():
        NotificationSettings.objects.create(pk=2)


def test_notification_migration_preserves_legacy_recipients_and_delivery_history() -> None:
    system, _ = configured()
    saved(system)
    old_target = [("notifications", "0001_initial")]
    executor = MigrationExecutor(connection)
    current_targets = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate(old_target)
        apps = executor.loader.project_state(old_target).apps
        preference_model = apps.get_model("notifications", "ReminderPreference")
        delivery_model = apps.get_model("notifications", "NotificationDelivery")
        preference = preference_model.objects.get(user_id=system.member.pk)
        preference.email = "legacy@example.com"
        preference.save()
        delivery = delivery_model.objects.create(
            preference=preference,
            scheduled_for=SLOT,
            available_at=SLOT,
            recipient="historic@example.com",
            status="sent",
            sent_at=SLOT,
            attempts=1,
        )
    finally:
        MigrationExecutor(connection).migrate(current_targets)
    restored = ReminderPreference.objects.get(pk=preference.pk)
    assert restored.recipients == ["legacy@example.com"]
    history = NotificationDelivery.objects.get(pk=delivery.pk)
    assert (history.recipient, history.status, history.sent_at, history.attempts) == (
        "historic@example.com",
        "sent",
        SLOT,
        1,
    )
    assert NotificationSettings.objects.get(pk=1).max_recipients == 5


def test_plant_recipient_cap_is_shared_distinct_and_private_between_members() -> None:
    system, preference = configured()
    addresses = [f"person{index}@example.com" for index in range(5)]
    configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        recipients=addresses,
        times=["10:00"],
        now=NOW,
    )
    other = User.objects.create(username="other", email="other@example.com")
    Membership.objects.create(user=other, tenant=system.tenant)
    with pytest.raises(ValidationError, match="in total"):
        configure_reminders(
            other,
            system.tenant.pk,
            system.region.pk,
            enabled=True,
            recipients=["other@example.com"],
            times=["11:00"],
            now=NOW,
        )
    own = configure_reminders(
        other,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        recipients=["PERSON0@example.com"],
        times=["11:00"],
        now=NOW,
    )
    payload = preference_payload(own, NOW)
    assert payload["recipient_count"] == 5
    assert payload["recipient_slots_remaining"] == 0
    assert payload["recipients"] == ["PERSON0@example.com"]
    assert "person1@example.com" not in str(payload)
    foreign = Tenant.objects.create(name="Foreign")
    with pytest.raises(PermissionDenied):
        configure_reminders(
            other,
            foreign.pk,
            system.region.pk,
            enabled=True,
            recipients=["intruder@example.com"],
            times=["10:00"],
            now=NOW,
        )
    preference.refresh_from_db()
    assert preference.recipients == addresses


def test_paused_lists_reserve_slots_and_clearing_all_settings_releases_them() -> None:
    system, _ = configured()
    preference = configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=False,
        recipients=["saved@example.com"],
        times=[],
        now=NOW,
    )
    assert preference_payload(preference, NOW)["recipient_count"] == 1
    preference = configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=False,
        recipients=[],
        times=[],
        now=NOW,
    )
    assert preference_payload(preference, NOW)["recipient_count"] == 0


def test_over_limit_lists_allow_gradual_reduction_and_disabling() -> None:
    system, _ = configured()
    configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        recipients=["a@example.com", "b@example.com", "c@example.com"],
        times=["10:00"],
        now=NOW,
    )
    other = User.objects.create(username="other", email="other@example.com")
    Membership.objects.create(user=other, tenant=system.tenant)
    configure_reminders(
        other,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        recipients=["d@example.com", "e@example.com"],
        times=["11:00"],
        now=NOW,
    )
    NotificationSettings.objects.filter(pk=1).update(max_recipients=2)
    paused = configure_reminders(
        other,
        system.tenant.pk,
        system.region.pk,
        enabled=False,
        recipients=["d@example.com", "e@example.com"],
        times=["11:00"],
        now=NOW,
    )
    assert preference_payload(paused, NOW)["recipient_count"] == 5
    reduced = configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        recipients=["a@example.com", "b@example.com"],
        times=["10:00"],
        now=NOW,
    )
    assert preference_payload(reduced, NOW)["recipient_limit_exceeded"] is True
    assert preference_payload(reduced, NOW)["recipient_count"] == 4
    with pytest.raises(ValidationError):
        configure_reminders(
            system.member,
            system.tenant.pk,
            system.region.pk,
            enabled=True,
            recipients=["a@example.com", "b@example.com", "c@example.com"],
            times=["10:00"],
            now=NOW,
        )


@pytest.mark.parametrize("change", ["inactive", "membership"])
def test_inactive_or_departed_members_do_not_reserve_plant_recipient_slots(change: str) -> None:
    system, preference = configured()
    if change == "inactive":
        User.objects.filter(pk=system.member.pk).update(is_active=False)
    else:
        Membership.objects.filter(user=system.member).delete()
    assert preference_payload(preference, NOW)["recipient_count"] == 0


def test_fallback_email_change_can_pause_every_member_at_plant_limit() -> None:
    system, _ = configured()
    other = User.objects.create(username="other", email="default@example.com")
    Membership.objects.create(user=other, tenant=system.tenant)
    configure_reminders(
        other,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        recipients=[],
        times=["10:00"],
        now=NOW,
    )
    NotificationSettings.objects.filter(pk=1).update(max_recipients=1)
    saved(system)
    ids = enqueue_due(SLOT)
    assert len(ids) == 2
    User.objects.filter(pk=other.pk).update(email="changed@example.com")
    assert not enqueue_due(SLOT + timedelta(days=1))
    assert all(not process_notification(delivery_id, SLOT) for delivery_id in ids)
    assert not mail.outbox


def test_concurrent_members_cannot_overbook_plant_recipient_capacity() -> None:
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
                    enabled=True,
                    recipients=[f"{user.username}{index}@example.com" for index in range(3)],
                    times=["10:00"],
                    now=NOW,
                )
            except ValidationError:
                return False
            return True
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(configure, [system.member, other]))
    assert sorted(outcomes) == [False, True]


@pytest.mark.parametrize(
    "addresses", [[], ["one@example.com"], ["one@example.com", "two@example.com"]]
)
def test_reminder_api_exposes_only_canonical_recipient_fields(addresses: list[str]) -> None:
    system, preference = configured()
    ReminderPreference.objects.filter(pk=preference.pk).update(recipients=addresses)
    client = APIClient()
    client.force_authenticate(system.member)
    response = client.get(f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/reminders/")
    assert response.status_code == 200
    payload = response.json()
    assert {"email", "effective_email"}.isdisjoint(payload)
    assert payload["recipients"] == addresses
    assert payload["effective_recipients"] == (addresses or ["default@example.com"])


def test_reminder_api_requires_an_explicit_recipient_list() -> None:
    system, _ = configured()
    client = APIClient()
    client.force_authenticate(system.member)
    response = client.put(
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/reminders/",
        {"enabled": True, "email": "obsolete@example.com", "times": ["10:00"]},
        format="json",
    )
    assert response.status_code == 400
    assert response.json() == {
        "code": "http_400",
        "detail": {"recipients": ["This field is required."]},
    }


def test_email_column_removal_preserves_recipients_and_pinned_deliveries() -> None:
    system, original = configured()
    saved(system)
    old_target = [("notifications", "0004_validate_delivery_status")]
    executor = MigrationExecutor(connection)
    current_targets = executor.loader.graph.leaf_nodes()
    expected = [
        ("legacy@example.com", [], ["legacy@example.com"]),
        ("single@example.com", ["single@example.com"], ["single@example.com"]),
        (
            "obsolete@example.com",
            ["one@example.com", "two@example.com"],
            ["one@example.com", "two@example.com"],
        ),
        ("", [], []),
    ]
    ids: list[int] = []
    try:
        executor.migrate(old_target)
        old_apps = executor.loader.project_state(old_target).apps
        preference_model = old_apps.get_model("notifications", "ReminderPreference")
        delivery_model = old_apps.get_model("notifications", "NotificationDelivery")
        preference_model.objects.filter(pk=original.pk).delete()
        for index, (email, recipients, _) in enumerate(expected):
            user = User.objects.create(username=f"legacy-{index}")
            preference = preference_model.objects.create(
                user_id=user.pk,
                region_id=system.region.pk,
                email=email,
                recipients=recipients,
                effective_from=NOW,
                times=["10:00"],
                enabled=False,
            )
            ids.append(preference.pk)
        delivery = delivery_model.objects.create(
            preference_id=ids[0],
            scheduled_for=SLOT,
            available_at=SLOT,
            recipient="pinned@example.com",
            status="uncertain",
            attempts=1,
        )
        MigrationExecutor(connection).migrate(current_targets)
        assert list(
            ReminderPreference.objects.filter(pk__in=ids)
            .order_by("pk")
            .values_list("recipients", flat=True)
        ) == [recipients for _, _, recipients in expected]
        history = NotificationDelivery.objects.get(pk=delivery.pk)
        assert (history.preference_id, history.recipient, history.status, history.attempts) == (
            ids[0],
            "pinned@example.com",
            "uncertain",
            1,
        )
        executor = MigrationExecutor(connection)
        executor.migrate(old_target)
        old_apps = executor.loader.project_state(old_target).apps
        preference_model = old_apps.get_model("notifications", "ReminderPreference")
        assert list(
            preference_model.objects.filter(pk__in=ids)
            .order_by("pk")
            .values_list("email", "recipients")
        ) == [
            ("legacy@example.com", ["legacy@example.com"]),
            ("single@example.com", ["single@example.com"]),
            ("", ["one@example.com", "two@example.com"]),
            ("", []),
        ]
    finally:
        MigrationExecutor(connection).migrate(current_targets)
