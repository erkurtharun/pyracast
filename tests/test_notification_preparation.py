from collections.abc import Sequence
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.core import mail
from django.core.mail import EmailMultiAlternatives
from django.db.models.fetch_modes import FetchMode
from openpyxl.worksheet.worksheet import Worksheet

from pyracast.connections.models import ProviderAccount
from pyracast.forecasts.models import (
    CollectionJob,
    CollectionSchedule,
    ForecastResult,
    ForecastSnapshot,
)
from pyracast.irradiance import local_day_bounds
from pyracast.notifications.jobs import process_notification
from pyracast.notifications.mail import NotSentError
from pyracast.notifications.models import NotificationDelivery
from pyracast.notifications.operations import configure_reminders
from pyracast.notifications.scheduler import PREPARATION_GRACE, enqueue_due
from pyracast.notifications.sources import eligible_saved_forecasts
from pyracast.notifications.views import reminder_schedule_payload
from pyracast.regions.models import Region
from tests.test_domain import NOW, System
from tests.test_notifications import (
    SLOT,
    attached_sheet,
    cache_saved_days,
    configured,
    memory_mail,
    saved,
)

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["memory_mail"]


def _summary_row(sheet: Worksheet, day: int) -> int:
    return next(
        row for row, cell in enumerate(sheet["A"], 1) if cell.value == datetime(2026, 11, day)
    )


def complete_saved(system: System, when: datetime = NOW) -> ForecastSnapshot:
    snapshot = saved(system, when)
    start, _ = local_day_bounds(
        SLOT.astimezone(ZoneInfo(system.region.timezone)).date(), system.binding.timezone
    )
    snapshot.normalized_intervals = [
        {
            "start": (start + timedelta(hours=hour)).isoformat(),
            "end": (start + timedelta(hours=hour + 1)).isoformat(),
            "power_kw": 0.3,
            "sunlight": "day",
        }
        for hour in range(48)
    ]
    snapshot.save()
    cache_saved_days(snapshot, when)
    return snapshot


@pytest.mark.parametrize("state", ["pending", "running", "retry", "missing"])
def test_same_clock_collection_waits_without_spending_mail_attempts_then_uses_publication(
    state: str,
) -> None:
    system, _ = configured()
    old = complete_saved(system)
    CollectionSchedule.objects.create(
        region=system.region, times=["10:00"], enabled=True, effective_from=NOW
    )
    job = None
    if state != "missing":
        job = CollectionJob.objects.create(
            binding=system.binding,
            scheduled_slot=SLOT,
            target_date=(SLOT + timedelta(days=1)).date(),
            available_at=SLOT,
            status=state,
        )
    delivery_id = enqueue_due(SLOT)[0]
    assert not process_notification(delivery_id, SLOT)
    delivery = NotificationDelivery.objects.get(pk=delivery_id)
    assert delivery.status == "pending"
    assert delivery.attempts == 0
    assert delivery.prepared_message is None
    assert not mail.outbox
    when = SLOT + timedelta(minutes=5)
    fresh = complete_saved(system, when)
    if job is not None:
        CollectionJob.objects.filter(pk=fresh.job_id).update(
            scheduled_slot=SLOT + timedelta(seconds=1)
        )
        ForecastSnapshot.objects.filter(pk=fresh.pk).update(job=job)
        CollectionJob.objects.filter(pk=job.pk).update(status="succeeded")
    else:
        CollectionJob.objects.filter(pk=fresh.job_id).update(scheduled_slot=SLOT)
    assert process_notification(delivery_id, when + timedelta(minutes=1))
    delivery = NotificationDelivery.objects.get(pk=delivery_id)
    assert delivery.snapshot_id == fresh.pk != old.pk
    assert isinstance(delivery.prepared_message, dict)
    assert delivery.prepared_message["kind"] == "current"
    assert (
        when + timedelta(minutes=1)
        <= datetime.fromisoformat(delivery.prepared_message["prepared_at"])
        < when + timedelta(minutes=2)
    )
    assert attached_sheet(mail.outbox[0])["A2"].value == when.astimezone(
        ZoneInfo(system.region.timezone)
    ).replace(tzinfo=None)
    assert "11 Nov 2026" in mail.outbox[0].subject


@pytest.mark.parametrize("job_state", ["failed", "stale", "succeeded"])
def test_terminal_collection_prepares_immediately_using_older_complete_forecast(
    job_state: str,
) -> None:
    system, _ = configured()
    old = complete_saved(system)
    CollectionJob.objects.create(
        binding=system.binding,
        scheduled_slot=SLOT,
        target_date=(SLOT + timedelta(days=1)).date(),
        available_at=SLOT,
        status=job_state,
    )
    delivery_id = enqueue_due(SLOT)[0]
    assert process_notification(delivery_id, SLOT)
    delivery = NotificationDelivery.objects.get(pk=delivery_id)
    assert delivery.snapshot_id == old.pk
    assert isinstance(delivery.prepared_message, dict)
    assert delivery.prepared_message["kind"] == "saved_complete"
    assert "Using the last saved forecast." in mail.outbox[0].body


def test_latest_partial_or_invalid_cache_cannot_hide_complete_saved_tomorrow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, _ = configured()
    old = complete_saved(system)
    partial = saved(system, SLOT - timedelta(minutes=10))
    bad = saved(system, SLOT - timedelta(minutes=5))
    ForecastResult.objects.filter(snapshot=bad).update(
        daily_payloads={"2026-11-11": {"energy_kwh_m2": "NaN"}}
    )
    ForecastSnapshot.objects.filter(pk=old.pk).update(
        normalized_intervals=[{"invalid": "archived source is not recalculated"}]
    )

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("Calculation is unavailable")

    monkeypatch.setattr("pyracast.calculation.calculate", broken)
    delivery_id = enqueue_due(SLOT)[0]
    assert process_notification(delivery_id, SLOT)
    delivery = NotificationDelivery.objects.get(pk=delivery_id)
    assert delivery.snapshot_id == old.pk != partial.pk != bad.pk
    sheet = attached_sheet(mail.outbox[0])
    assert sheet.cell(_summary_row(sheet, 11), 3).value == pytest.approx(
        ForecastResult.objects.get(snapshot=old).daily_payloads["2026-11-11"]["daylight"][
            "energy_kwh_m2"
        ]
    )


def test_restart_respects_fixed_publication_cutoff_and_hard_send_deadline() -> None:
    system, _ = configured()
    old = complete_saved(system)
    late = complete_saved(system, SLOT + PREPARATION_GRACE + timedelta(seconds=1))
    CollectionJob.objects.filter(pk=late.job_id).update(scheduled_slot=SLOT)
    delivery_id = enqueue_due(SLOT + timedelta(minutes=90))[0]
    assert process_notification(delivery_id, SLOT + timedelta(minutes=90))
    delivery = NotificationDelivery.objects.get(pk=delivery_id)
    assert delivery.snapshot_id == old.pk
    assert delivery.preparation_deadline == SLOT + PREPARATION_GRACE


def test_missing_due_job_waits_only_to_fixed_deadline_then_sends_honest_unavailable() -> None:
    system, _ = configured()
    CollectionSchedule.objects.create(
        region=system.region, times=["10:00"], enabled=True, effective_from=NOW
    )
    delivery_id = enqueue_due(SLOT)[0]
    assert not process_notification(delivery_id, SLOT + PREPARATION_GRACE - timedelta(seconds=1))
    delivery = NotificationDelivery.objects.get(pk=delivery_id)
    assert delivery.available_at == SLOT + PREPARATION_GRACE
    assert process_notification(delivery_id, SLOT + PREPARATION_GRACE)
    delivery.refresh_from_db()
    assert delivery.snapshot_id is None
    assert delivery.binding_id == system.binding.pk
    assert isinstance(delivery.prepared_message, dict)
    assert delivery.prepared_message["kind"] == "unavailable"
    assert "Tomorrow's forecast is unavailable" in mail.outbox[0].body
    sheet = attached_sheet(mail.outbox[0])
    assert sheet.cell(_summary_row(sheet, 11), 3).value is None


def test_credentials_rotated_or_auth_failed_preserves_approved_archived_email() -> None:
    system, _ = configured()
    snapshot = complete_saved(system)
    ProviderAccount.objects.filter(pk=system.account.pk).update(
        credential_revision=2, authentication_failed_revision=2
    )
    delivery_id = enqueue_due(SLOT)[0]
    assert process_notification(delivery_id, SLOT)
    assert NotificationDelivery.objects.get(pk=delivery_id).snapshot_id == snapshot.pk


def test_shared_recipients_and_retries_keep_the_same_prepared_content() -> None:
    system, _ = configured()
    old = complete_saved(system)
    configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        recipients=["default@example.com", "second@example.com"],
        times=["10:00"],
        language="en",
        now=NOW,
    )
    (delivery_id,) = enqueue_due(SLOT)
    attempted: list[EmailMultiAlternatives] = []

    def fail(message: EmailMultiAlternatives) -> None:
        attempted.append(message)
        raise NotSentError

    assert not process_notification(delivery_id, SLOT, sender=fail)
    delivery = NotificationDelivery.objects.get(pk=delivery_id)
    original = delivery.prepared_message
    assert original is not None
    assert "second@example.com" not in str(original)
    assert attempted[0].bcc == ["default@example.com", "second@example.com"]
    complete_saved(system, SLOT - timedelta(minutes=1))
    Region.objects.filter(pk=system.region.pk).update(name="Changed after preparation")
    assert process_notification(delivery.pk, delivery.available_at)
    delivery.refresh_from_db()
    assert delivery.snapshot_id == old.pk
    assert delivery.prepared_message == original
    assert delivery.attempts == 2
    assert len(mail.outbox) == 1
    assert mail.outbox[0].body == attempted[0].body
    assert mail.outbox[0].attachments == attempted[0].attachments
    assert mail.outbox[0].bcc == attempted[0].bcc


def test_partial_tomorrow_survives_missing_today_and_does_not_invent_daily_total() -> None:
    system, _ = configured()
    saved(system)
    newer = saved(system, SLOT - timedelta(minutes=1))
    result = newer.forecastresult
    result.daily_payloads.pop("2026-11-10")
    result.save()
    delivery_id = enqueue_due(SLOT)[0]
    assert process_notification(delivery_id, SLOT)
    delivery = NotificationDelivery.objects.get(pk=delivery_id)
    assert delivery.snapshot_id == newer.pk
    assert isinstance(delivery.prepared_message, dict)
    assert delivery.prepared_message["kind"] == "saved_partial"
    assert "Unavailable" in mail.outbox[0].body
    assert "Partial:" in mail.outbox[0].body
    sheet = attached_sheet(mail.outbox[0])
    assert sheet.cell(_summary_row(sheet, 10), 3).value is None
    row = _summary_row(sheet, 11)
    assert sheet.cell(row, 3).value == pytest.approx(
        result.daily_payloads["2026-11-11"]["daylight"]["partial_energy_kwh_m2"]
    )
    note = str(sheet.cell(row + 1, 1).value)
    coverage = result.daily_payloads["2026-11-11"]["daylight"]["coverage_fraction"]
    assert f"Coverage: {coverage:.0%}" in note
    assert "Partial estimate" in note
    assert delivery.prepared_message["tomorrow_complete"] is False


def test_legacy_unprepared_recipient_reuses_existing_slot_content() -> None:
    system, schedule = configured()
    snapshot = complete_saved(system)
    delivery_id = enqueue_due(SLOT)[0]
    assert process_notification(delivery_id, SLOT)
    extra = NotificationDelivery.objects.create(
        schedule=schedule,
        binding=system.binding,
        snapshot=snapshot,
        scheduled_for=SLOT,
        recipients=["legacy@example.com"],
        grouped=False,
        available_at=SLOT,
    )
    schedule.recipients = ["legacy@example.com"]
    schedule.save()
    assert process_notification(extra.pk, SLOT)
    original = NotificationDelivery.objects.get(pk=delivery_id)
    extra.refresh_from_db()
    assert original.prepared_message == extra.prepared_message
    assert mail.outbox[0].body == mail.outbox[1].body


def test_legacy_preparation_uses_slot_cutoff_without_grace() -> None:
    system, _ = configured()
    old = complete_saved(system)
    delivery_id = enqueue_due(SLOT)[0]
    NotificationDelivery.objects.filter(pk=delivery_id).update(preparation_deadline=None)
    newer = complete_saved(system, SLOT + timedelta(seconds=30))
    CollectionJob.objects.filter(pk=newer.job_id).update(scheduled_slot=SLOT)
    assert process_notification(delivery_id, SLOT + timedelta(minutes=1))
    assert NotificationDelivery.objects.get(pk=delivery_id).snapshot_id == old.pk


@pytest.mark.parametrize(
    "change", ["actuals", "future_acquisition", "late_publication", "failed_job"]
)
def test_ineligible_archived_sources_send_unavailable_without_exposing_their_values(
    change: str,
) -> None:
    system, _ = configured()
    snapshot = complete_saved(system)
    if change == "actuals":
        ForecastSnapshot.objects.filter(pk=snapshot.pk).update(source_kind="estimated_actuals")
    elif change == "future_acquisition":
        ForecastSnapshot.objects.filter(pk=snapshot.pk).update(
            collected_at=SLOT + timedelta(seconds=1)
        )
    elif change == "late_publication":
        ForecastResult.objects.filter(snapshot=snapshot).update(
            published_at=SLOT + PREPARATION_GRACE + timedelta(seconds=1)
        )
    else:
        CollectionJob.objects.filter(pk=snapshot.job_id).update(status="failed")
    delivery_id = enqueue_due(SLOT)[0]
    assert process_notification(delivery_id, SLOT + timedelta(minutes=1))
    delivery = NotificationDelivery.objects.get(pk=delivery_id)
    assert delivery.snapshot_id is None
    assert "Tomorrow's forecast is unavailable" in mail.outbox[0].body
    sheet = attached_sheet(mail.outbox[0])
    assert sheet.cell(_summary_row(sheet, 11), 3).value is None


def test_same_slot_partial_forecast_exposes_incomplete_saved_data_status() -> None:
    system, schedule = configured()
    snapshot = saved(system, SLOT + timedelta(minutes=1))
    delivery_id = enqueue_due(SLOT)[0]
    before = reminder_schedule_payload(schedule, SLOT)["deliveries"]
    assert isinstance(before, list)
    assert before[0]["forecast_status"] is None
    assert before[0]["forecast_complete"] is None
    assert process_notification(delivery_id, SLOT + timedelta(minutes=2))
    delivery = NotificationDelivery.objects.get(pk=delivery_id)
    assert delivery.snapshot_id == snapshot.pk
    assert isinstance(delivery.prepared_message, dict)
    assert delivery.prepared_message["kind"] == "saved_partial"
    assert delivery.prepared_message["tomorrow_complete"] is False
    after = reminder_schedule_payload(schedule, SLOT + timedelta(minutes=2))["deliveries"]
    assert isinstance(after, list)
    assert after[0]["status"] == "sent"
    assert after[0]["forecast_status"] == "saved_partial"
    assert after[0]["forecast_complete"] is False


@pytest.mark.parametrize("complete", [True, False])
def test_delivery_api_separates_complete_and_unavailable_forecasts(complete: bool) -> None:
    system, schedule = configured()
    if complete:
        complete_saved(system)
    delivery_id = enqueue_due(SLOT)[0]
    assert process_notification(delivery_id, SLOT)
    payload = reminder_schedule_payload(schedule, SLOT)["deliveries"]
    assert isinstance(payload, list)
    assert payload[0]["forecast_status"] == ("saved_complete" if complete else "unavailable")
    assert payload[0]["forecast_complete"] is complete


def test_candidate_loading_excludes_unrelated_history_and_defers_provider_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, _ = configured()
    usable = saved(system)
    jobs = CollectionJob.objects.bulk_create(
        [
            CollectionJob(
                binding=system.binding,
                scheduled_slot=NOW - timedelta(days=index + 1),
                target_date=(NOW - timedelta(days=index)).date(),
                available_at=NOW,
                status="succeeded",
            )
            for index in range(30)
        ]
    )
    historical = ForecastSnapshot.objects.bulk_create(
        [
            ForecastSnapshot(
                binding=system.binding,
                job=job,
                collected_at=job.scheduled_slot,
                available_at=job.scheduled_slot,
                source_kind="forecast",
                raw_payload={"unused": "history"},
                normalized_intervals=[],
            )
            for job in jobs
        ]
    )
    ForecastResult.objects.bulk_create(
        [
            ForecastResult(
                snapshot=snapshot,
                payload={"local_date": snapshot.job.target_date.isoformat()},
                daily_payloads={},
                published_at=snapshot.collected_at,
            )
            for snapshot in historical
        ]
    )
    delivery_id = enqueue_due(SLOT)[0]
    original = ForecastSnapshot.from_db
    loaded: list[int] = []

    def load(
        model: type[ForecastSnapshot],
        database: str,
        field_names: list[str],
        values: Sequence[object],
        *,
        fetch_mode: FetchMode | None = None,
    ) -> ForecastSnapshot:
        snapshot = original(database, field_names, values, fetch_mode=fetch_mode)
        loaded.append(snapshot.pk)
        assert snapshot.pk == usable.pk
        assert snapshot.get_deferred_fields() == {
            "raw_payload",
            "normalized_intervals",
            "weather_payload",
        }
        return snapshot

    monkeypatch.setattr(ForecastSnapshot, "from_db", classmethod(load))
    assert process_notification(delivery_id, SLOT)
    assert loaded == [usable.pk]
    assert "Partial:" in mail.outbox[0].body


def test_legacy_exact_day_payload_remains_eligible_without_daily_cache() -> None:
    system, _ = configured()
    snapshot = complete_saved(system, SLOT - timedelta(hours=1))
    result = ForecastResult.objects.get(snapshot=snapshot)
    result.payload = result.daily_payloads["2026-11-11"]
    result.daily_payloads = {}
    result.save()
    delivery_id = enqueue_due(SLOT)[0]
    assert process_notification(delivery_id, SLOT)
    assert NotificationDelivery.objects.get(pk=delivery_id).snapshot_id == snapshot.pk
    sheet = attached_sheet(mail.outbox[0])
    assert sheet.cell(_summary_row(sheet, 11), 3).value == pytest.approx(
        result.payload["daylight"]["energy_kwh_m2"]
    )


def test_default_collection_clock_waits_without_an_explicit_schedule_row() -> None:
    system, _ = configured()
    complete_saved(system)
    configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        language="en",
        enabled=True,
        recipients=[],
        times=["16:00"],
        now=NOW,
    )
    slot = SLOT + timedelta(hours=6)
    enqueue_due(slot)
    delivery = NotificationDelivery.objects.get(schedule__region=system.region, scheduled_for=slot)
    assert not CollectionSchedule.objects.filter(region=system.region).exists()
    assert not CollectionJob.objects.filter(binding=system.binding, scheduled_slot=slot).exists()
    assert not process_notification(delivery.pk, slot)
    delivery.refresh_from_db()
    assert delivery.prepared_message is None
    assert delivery.attempts == 0
    assert process_notification(delivery.pk, slot + PREPARATION_GRACE)


def test_collection_retry_after_preparation_deadline_does_not_delay_saved_mail() -> None:
    system, _ = configured()
    complete_saved(system)
    CollectionJob.objects.create(
        binding=system.binding,
        scheduled_slot=SLOT,
        target_date=(SLOT + timedelta(days=1)).date(),
        status="retry",
        available_at=SLOT + PREPARATION_GRACE,
    )
    delivery_id = enqueue_due(SLOT)[0]
    assert process_notification(delivery_id, SLOT)
    assert "Using the last saved forecast." in mail.outbox[0].body


def test_notification_settings_remain_editable_after_provider_credentials_rotate() -> None:
    system, _ = configured()
    ProviderAccount.objects.filter(pk=system.account.pk).update(
        credential_revision=2, authentication_failed_revision=2
    )
    schedule = configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        language="en",
        enabled=True,
        recipients=["updated@example.com"],
        times=["11:00"],
        now=NOW,
    )
    assert schedule.enabled
    assert schedule.recipients == ["updated@example.com"]
    assert schedule.times == ["11:00"]


def test_forecast_age_in_history_uses_preparation_time_instead_of_view_time() -> None:
    system, schedule = configured()
    complete_saved(system)
    delivery_id = enqueue_due(SLOT)[0]
    assert process_notification(delivery_id, SLOT)
    delivery = NotificationDelivery.objects.get(pk=delivery_id)
    assert isinstance(delivery.prepared_message, dict)
    prepared_at = datetime.fromisoformat(delivery.prepared_message["prepared_at"])
    at_send = reminder_schedule_payload(schedule, SLOT)["deliveries"]
    later = reminder_schedule_payload(schedule, SLOT + timedelta(days=7))["deliveries"]
    assert isinstance(at_send, list)
    assert isinstance(later, list)
    assert (
        at_send[0]["source_age_seconds"]
        == later[0]["source_age_seconds"]
        == (prepared_at - NOW).total_seconds()
    )
    assert at_send[0]["stale"] is later[0]["stale"] is False


@pytest.mark.parametrize("prepared_at", ["invalid", "2026-11-10T07:00:00", 7])
def test_legacy_history_with_invalid_preparation_metadata_uses_sent_time(
    prepared_at: object,
) -> None:
    system, schedule = configured()
    saved(system)
    delivery_id = enqueue_due(SLOT)[0]
    NotificationDelivery.objects.filter(pk=delivery_id).update(
        status="sent", sent_at=SLOT, prepared_message={"prepared_at": prepared_at}
    )
    payload = reminder_schedule_payload(schedule, SLOT + timedelta(days=7))["deliveries"]
    assert isinstance(payload, list)
    assert payload[0]["source_age_seconds"] == (SLOT - NOW).total_seconds()
    assert payload[0]["stale"] is False


def test_a_saved_forecast_is_eligible_only_once_its_snapshot_is_available() -> None:
    system, _ = configured()
    snapshot = saved(system)
    delivery = NotificationDelivery.objects.get(pk=enqueue_due(SLOT)[0])
    ForecastSnapshot.objects.filter(pk=snapshot.pk).update(available_at=NOW)
    assert list(eligible_saved_forecasts(delivery, SLOT)) == [snapshot]
    ForecastSnapshot.objects.filter(pk=snapshot.pk).update(available_at=None)
    assert not eligible_saved_forecasts(delivery, SLOT).exists()
