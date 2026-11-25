import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.core import mail
from django.core.mail import EmailMessage
from django.db.models.fetch_modes import FetchMode
from openpyxl.worksheet.worksheet import Worksheet

from pyracast.connections.models import ProviderAccount
from pyracast.forecasts.models import (
    CollectionJob,
    CollectionSchedule,
    ForecastResult,
    ForecastSnapshot,
    HistoricalReferenceImport,
)
from pyracast.mail.errors import NotSentError
from pyracast.notifications.jobs import process_notification
from pyracast.notifications.models import NotificationDelivery
from pyracast.notifications.operations import configure_reminders
from pyracast.notifications.scheduler import PREPARATION_GRACE, enqueue_due
from pyracast.notifications.sources import eligible_saved_forecasts
from pyracast.regions.models import Region
from tests.support.domain import NOW
from tests.support.invariants import stored_again
from tests.support.notification_preparation import complete_saved
from tests.support.notification_transmission import replying
from tests.support.notifications import (
    SLOT,
    attached_sheet,
    configured,
    reminder_schedule_payload,
    saved,
    schedule_addresses,
)

pytestmark = pytest.mark.django_db(transaction=True)


def _summary_row(sheet: Worksheet, day: int) -> int:
    return next(
        row for row, cell in enumerate(sheet["A"], 1) if cell.value == datetime(2026, 11, day)
    )


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
            scheduled_for=SLOT,
            target_date=(SLOT + timedelta(days=1)).date(),
            available_at=SLOT,
            status=state,
            lease_token=uuid.uuid4() if state == "running" else None,
            lease_expires_at=SLOT + timedelta(minutes=5) if state == "running" else None,
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
            scheduled_for=SLOT + timedelta(seconds=1)
        )
        stored_again(ForecastSnapshot.objects.get(pk=fresh.pk), job=job)
        CollectionJob.objects.filter(pk=job.pk).update(
            status="succeeded", lease_token=None, lease_expires_at=None
        )
    else:
        CollectionJob.objects.filter(pk=fresh.job_id).update(scheduled_for=SLOT)
    assert process_notification(delivery_id, when + timedelta(minutes=1))
    delivery = NotificationDelivery.objects.get(pk=delivery_id)
    assert delivery.snapshot_id == fresh.pk != old.pk
    assert isinstance(delivery.prepared_message, dict)
    assert delivery.forecast_kind == "current"
    assert delivery.prepared_at is not None
    assert when + timedelta(minutes=1) <= delivery.prepared_at < when + timedelta(minutes=2)
    assert attached_sheet(mail.outbox[0])["B4"].value == when.astimezone(
        ZoneInfo(system.region.timezone)
    ).replace(tzinfo=None)
    assert "Wed 11 Nov" in mail.outbox[0].subject
    assert "\n\nForecast data: 10 Nov 2026, 10:05 (UTC+3)\n" in mail.outbox[0].body
    assert "Newer forecast data" not in mail.outbox[0].body


@pytest.mark.parametrize("job_state", ["failed", "stale", "succeeded"])
def test_terminal_collection_prepares_immediately_using_older_complete_forecast(
    job_state: str,
) -> None:
    system, _ = configured()
    old = complete_saved(system)
    CollectionSchedule.objects.create(
        region=system.region, times=["10:00"], enabled=True, effective_from=NOW
    )
    CollectionJob.objects.create(
        binding=system.binding,
        scheduled_for=SLOT,
        target_date=(SLOT + timedelta(days=1)).date(),
        available_at=SLOT,
        status=job_state,
    )
    delivery_id = enqueue_due(SLOT)[0]
    assert process_notification(delivery_id, SLOT)
    delivery = NotificationDelivery.objects.get(pk=delivery_id)
    assert delivery.snapshot_id == old.pk
    assert isinstance(delivery.prepared_message, dict)
    assert delivery.forecast_kind == "saved_complete"
    assert (
        "Newer forecast data was not available.\nForecast data: 9 Nov 2026, 16:00 (UTC+3)\n"
    ) in mail.outbox[0].body
    sheet = attached_sheet(mail.outbox[0])
    assert sheet["A12"].value == "Newer forecast data was not available."


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
    stored_again(
        ForecastSnapshot.objects.get(pk=old.pk),
        normalized_intervals=[{"invalid": "archived source is not recalculated"}],
    )

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("Calculation is unavailable")

    monkeypatch.setattr("pyracast.calculation.calculate", broken)
    delivery_id = enqueue_due(SLOT)[0]
    assert process_notification(delivery_id, SLOT)
    delivery = NotificationDelivery.objects.get(pk=delivery_id)
    assert delivery.snapshot_id == old.pk != partial.pk != bad.pk
    sheet = attached_sheet(mail.outbox[0])
    assert sheet.cell(_summary_row(sheet, 11), 2).value == pytest.approx(
        ForecastResult.objects.get(snapshot=old).daily_payloads["2026-11-11"]["daylight"][
            "energy_kwh_m2"
        ]
    )


def test_restart_respects_fixed_publication_cutoff_and_hard_send_deadline() -> None:
    system, _ = configured()
    old = complete_saved(system)
    late = complete_saved(system, SLOT + PREPARATION_GRACE + timedelta(seconds=1))
    CollectionJob.objects.filter(pk=late.job_id).update(scheduled_for=SLOT)
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
    assert delivery.forecast_kind == "unavailable"
    assert mail.outbox[0].subject.endswith(": no forecast for Wed 11 Nov")
    assert "This email contains no figures." in mail.outbox[0].body
    assert "Forecast data" not in mail.outbox[0].body
    assert not mail.outbox[0].attachments


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
    attempted: list[EmailMessage] = []

    def fail(message: EmailMessage) -> None:
        attempted.append(message)
        raise NotSentError

    with replying(fail):
        assert not process_notification(delivery_id, SLOT)
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
    assert delivery.forecast_kind == "saved_partial"
    body = mail.outbox[0].body
    assert "Today (Tue 10 Nov 2026): No forecast data for this day." in body
    assert "Tomorrow (Wed 11 Nov 2026): Data is missing for some hours, so no daily total" in body
    partial = result.daily_payloads["2026-11-11"]["daylight"]["partial_energy_kwh_m2"]
    assert f"{partial:.2f}" not in body
    sheet = attached_sheet(mail.outbox[0])
    assert sheet.cell(_summary_row(sheet, 10), 2).value is None
    assert sheet.cell(_summary_row(sheet, 11), 2).value is None
    assert [sheet.cell(row, 1).value for row in (12, 13)] == [
        "Tue 10 Nov 2026: no forecast data.",
        "Wed 11 Nov 2026: data is missing for some hours; no daily total.",
    ]
    assert delivery.forecast_complete is False


@pytest.mark.parametrize(
    "change", ["actuals", "future_acquisition", "late_publication", "failed_job"]
)
def test_ineligible_archived_sources_send_unavailable_without_exposing_their_values(
    change: str,
) -> None:
    system, _ = configured()
    snapshot = complete_saved(system)
    if change == "actuals":
        stored_again(ForecastSnapshot.objects.get(pk=snapshot.pk), source_kind="estimated_actuals")
    elif change == "future_acquisition":
        stored_again(
            ForecastSnapshot.objects.get(pk=snapshot.pk), collected_at=SLOT + timedelta(seconds=1)
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
    assert mail.outbox[0].subject.endswith(": no forecast for Wed 11 Nov")
    assert not mail.outbox[0].attachments


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
    assert delivery.forecast_kind == "saved_partial"
    assert delivery.forecast_complete is False
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
                scheduled_for=NOW - timedelta(days=index + 1),
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
                collected_at=job.scheduled_for,
                available_at=job.scheduled_for,
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
        }
        return snapshot

    monkeypatch.setattr(ForecastSnapshot, "from_db", classmethod(load))
    assert process_notification(delivery_id, SLOT)
    assert loaded == [usable.pk]
    assert "Data is missing for some hours" in mail.outbox[0].body


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
    assert sheet.cell(_summary_row(sheet, 11), 2).value == pytest.approx(
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
        recipients=["default@example.com"],
        times=["16:00"],
        now=NOW,
    )
    slot = SLOT + timedelta(hours=6)
    enqueue_due(slot)
    delivery = NotificationDelivery.objects.get(schedule__region=system.region, scheduled_for=slot)
    assert not CollectionSchedule.objects.filter(region=system.region).exists()
    assert not CollectionJob.objects.filter(binding=system.binding, scheduled_for=slot).exists()
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
        scheduled_for=SLOT,
        target_date=(SLOT + timedelta(days=1)).date(),
        status="retry",
        available_at=SLOT + PREPARATION_GRACE,
    )
    delivery_id = enqueue_due(SLOT)[0]
    assert process_notification(delivery_id, SLOT)
    assert "\n\nForecast data: 9 Nov 2026, 16:00 (UTC+3)\n" in mail.outbox[0].body
    assert "Newer forecast data" not in mail.outbox[0].body


@pytest.mark.parametrize(("hours", "stale"), [(20, False), (27, True)])
def test_a_reminder_clock_without_a_collection_calls_only_old_data_not_current(
    hours: int, stale: bool
) -> None:
    system, _ = configured()
    collected = SLOT - timedelta(hours=hours)
    complete_saved(system, collected)
    delivery_id = enqueue_due(SLOT)[0]
    assert process_notification(delivery_id, SLOT)
    body = mail.outbox[0].body
    local = collected.astimezone(ZoneInfo(system.region.timezone))
    line = f"Forecast data: {local.day} Nov 2026, {local:%H:%M} (UTC+3)\n"
    notice = "Newer forecast data was not available.\n"
    assert (notice + line if stale else "\n\n" + line) in body
    assert ("Newer" in body) is stale
    assert body.count("2026, ") == 1


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
    assert schedule_addresses(schedule) == ["updated@example.com"]
    assert schedule.times == ["11:00"]


def test_forecast_age_in_history_uses_preparation_time_instead_of_view_time() -> None:
    system, schedule = configured()
    complete_saved(system)
    delivery_id = enqueue_due(SLOT)[0]
    assert process_notification(delivery_id, SLOT)
    delivery = NotificationDelivery.objects.get(pk=delivery_id)
    prepared_at = delivery.prepared_at
    assert prepared_at is not None
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


def test_history_without_a_preparation_time_measures_the_source_age_at_sending() -> None:
    system, schedule = configured()
    saved(system)
    delivery_id = enqueue_due(SLOT)[0]
    NotificationDelivery.objects.filter(pk=delivery_id).update(status="sent", sent_at=SLOT)
    payload = reminder_schedule_payload(schedule, SLOT + timedelta(days=7))["deliveries"]
    assert isinstance(payload, list)
    assert payload[0]["source_age_seconds"] == (SLOT - NOW).total_seconds()
    assert payload[0]["stale"] is False


def test_a_saved_forecast_is_eligible_only_once_its_snapshot_is_available() -> None:
    system, _ = configured()
    snapshot = saved(system)
    delivery = NotificationDelivery.objects.get(pk=enqueue_due(SLOT)[0])
    stored_again(ForecastSnapshot.objects.get(pk=snapshot.pk), available_at=NOW)
    assert list(eligible_saved_forecasts(delivery, SLOT)) == [snapshot]
    stored_again(ForecastSnapshot.objects.get(pk=snapshot.pk), available_at=None)
    assert not eligible_saved_forecasts(delivery, SLOT).exists()


def test_two_clocks_a_day_send_two_editions_each_naming_its_own_clock() -> None:
    system, _ = configured()
    complete_saved(system)
    configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        language="en",
        enabled=True,
        recipients=["default@example.com"],
        times=["10:00", "12:00"],
        now=NOW,
    )
    for slot in (SLOT, SLOT + timedelta(hours=2)):
        delivery_id = enqueue_due(slot)[-1]
        assert process_notification(delivery_id, slot)
    morning, noon = mail.outbox
    assert morning.subject.endswith(": irradiance forecast for Wed 11 Nov (10:00 edition)")
    assert noon.subject.endswith(": irradiance forecast for Wed 11 Nov (12:00 edition)")
    assert morning.attachments[0][0].endswith("-2026-11-11-1000.xlsx")
    assert noon.attachments[0][0].endswith("-2026-11-11-1200.xlsx")
    assert morning.extra_headers["Message-ID"] != noon.extra_headers["Message-ID"]


def test_a_clock_before_the_schedule_took_effect_does_not_make_the_only_send_an_edition() -> None:
    system, _ = configured()
    complete_saved(system)
    configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        language="en",
        enabled=True,
        recipients=["default@example.com"],
        times=["10:00", "12:00"],
        now=SLOT + timedelta(hours=1),
    )
    noon = SLOT + timedelta(hours=2)
    (delivery_id,) = enqueue_due(noon)
    assert process_notification(delivery_id, noon)
    (message,) = mail.outbox
    assert message.subject.endswith(": irradiance forecast for Wed 11 Nov")
    assert message.attachments[0][0].endswith("-2026-11-11.xlsx")


@pytest.mark.parametrize("source", ["own partial", "reference import"])
def test_at_a_collection_clock_only_a_snapshot_of_another_source_is_not_current(
    source: str,
) -> None:
    system, _ = configured()
    CollectionSchedule.objects.create(
        region=system.region, times=["10:00"], enabled=True, effective_from=NOW
    )
    collected = (
        SLOT + timedelta(minutes=1) if source == "own partial" else SLOT - timedelta(hours=2)
    )
    snapshot = saved(system, collected)
    if source == "reference import":
        job = snapshot.job
        job.reference_import = HistoricalReferenceImport.objects.create(
            binding=system.binding,
            content_sha256="a" * 64,
            source_name="authorized-export.json",
            source_settings={"product": "historical_rooftop_pv"},
            imported_by=system.admin,
            created_at=collected,
            interval_count=48,
        )
        job.endpoint, job.scheduled_for = "historical_import", SLOT
        job.save()
        CollectionJob.objects.create(
            binding=system.binding,
            scheduled_for=SLOT,
            target_date=(SLOT + timedelta(days=1)).date(),
            available_at=SLOT,
            status="failed",
        )
    delivery_id = enqueue_due(SLOT)[0]
    assert process_notification(delivery_id, SLOT + timedelta(minutes=2))
    delivery = NotificationDelivery.objects.get(pk=delivery_id)
    assert delivery.snapshot_id == snapshot.pk
    body = mail.outbox[0].body
    local = collected.astimezone(ZoneInfo(system.region.timezone))
    line = f"Forecast data: 10 Nov 2026, {local:%H:%M} (UTC+3)\n"
    if source == "own partial":
        assert delivery.forecast_kind == "saved_partial"
        assert "\n\n" + line in body
        assert "Newer forecast data" not in body
    else:
        assert "\nNewer forecast data was not available.\n" + line in body
