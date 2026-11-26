from base64 import b64decode
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from django.core import mail
from django.utils import timezone

from pyracast.accounts.models import Tenant
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import (
    CollectionJob,
    ForecastResult,
    ForecastSnapshot,
    RequestAttempt,
)
from pyracast.irradiance import local_day_bounds
from pyracast.notifications.jobs import process_notification
from pyracast.notifications.models import NotificationDelivery
from pyracast.notifications.preparation import prepare_delivery
from pyracast.notifications.report_data import saved_report_context
from pyracast.notifications.sources import saved_report_results
from pyracast.plants.models import Plant, PlantConfiguration
from pyracast.regions.models import Region
from tests.support.domain import System
from tests.support.incident_calibration import incident
from tests.support.invariants import stored_again
from tests.support.notification_report_data import region_delivery, report_delivery
from tests.support.notifications import SLOT, cache_saved_days
from tests.support.plant_operations import publish_snapshot, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)


def intraday_delivery(
    monkeypatch: pytest.MonkeyPatch, *, archived_morning: bool = True
) -> tuple[System, Plant, NotificationDelivery, ForecastSnapshot | None]:
    system, plant, delivery = report_delivery(monkeypatch)
    return system, plant, delivery, intraday_sources(system, delivery, archived_morning)


def intraday_sources(
    system: System, delivery: NotificationDelivery, archived_morning: bool = True
) -> ForecastSnapshot | None:
    day = SLOT.astimezone(ZoneInfo(system.binding.timezone)).date()
    start, end = local_day_bounds(day, system.binding.timezone)
    old = (
        publish_snapshot(
            snapshot_for(system, day, power_kw=0.1, collected_at=start - timedelta(hours=1))
        )
        if archived_morning
        else None
    )
    latest = delivery.snapshot
    assert latest is not None
    latest.collected_at = SLOT - timedelta(minutes=30)
    latest.available_at = latest.collected_at
    latest.issued_at = latest.collected_at
    latest.coverage_start = start + timedelta(hours=9, minutes=30)
    latest.coverage_end = end + timedelta(days=1)
    latest.normalized_intervals = [
        {
            "start": (latest.coverage_start + timedelta(minutes=30 * index)).isoformat(),
            "end": (latest.coverage_start + timedelta(minutes=30 * (index + 1))).isoformat(),
            "power_kw": 0.2,
        }
        for index in range(77)
    ]
    stored_again(latest)
    cache_saved_days(latest, latest.collected_at)
    return old


def test_today_report_composes_archived_morning_and_intraday_forecast_before_aggregation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, delivery, old = intraday_delivery(monkeypatch)
    assert old is not None
    monkeypatch.setattr(timezone, "now", lambda: SLOT - timedelta(minutes=1))
    start, _ = local_day_bounds(
        SLOT.astimezone(ZoneInfo(system.binding.timezone)).date(), system.binding.timezone
    )
    old.normalized_intervals[18]["power_kw"] = 2
    stored_again(old)
    outage = incident(system, plant, start, None)
    counts = (
        RequestAttempt.objects.count(),
        CollectionJob.objects.count(),
        ForecastSnapshot.objects.count(),
    )
    days = cast(list[dict[str, Any]], saved_report_context(delivery)["days"])
    today, tomorrow = days
    assert today["normal"]["energy_mwh"] == pytest.approx(544)
    assert today["normal"]["daylight"]["energy_mwh"] == pytest.approx(290.5)
    assert today["outage"]["daylight"]["energy_mwh"] == pytest.approx(170.25)
    assert today["incident_revision_ids"] == [outage.revisions.get().pk]
    assert today["normal"]["provenance"]["source_snapshot_ids"] == [delivery.snapshot_id, old.pk]
    assert today["normal"]["provenance"]["selection_policy"] == "latest-forecast-coverage-v2"
    assert "mixed_forecast_vintages" in today["normal"]["quality_flags"]
    boundary = next(
        row
        for row in today["normal"]["report_intervals"]
        if datetime.fromisoformat(row["start"]) == start + timedelta(hours=9)
    )
    assert boundary["energy_mwh"] == pytest.approx(63)
    outage_boundary = next(
        row for row in today["outage"]["report_intervals"] if row["start"] == boundary["start"]
    )
    assert outage_boundary["energy_mwh"] == pytest.approx(56.5)
    for key in ("normal", "outage"):
        result = today[key]
        assert sum(
            row["partial_energy_mwh"] for row in result["report_intervals"]
        ) == pytest.approx(result["daylight"]["partial_energy_mwh"])
    assert tomorrow["normal"]["energy_mwh"] == pytest.approx(624)
    assert tomorrow["normal"]["provenance"]["snapshot_id"] == delivery.snapshot_id
    assert counts == (
        RequestAttempt.objects.count(),
        CollectionJob.objects.count(),
        ForecastSnapshot.objects.count(),
    )


@pytest.mark.parametrize("value", ["missing", "null", "zero"])
def test_today_report_never_fills_new_forecast_missingness_with_older_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    _, _, delivery, old = intraday_delivery(monkeypatch)
    assert old is not None
    latest = delivery.snapshot
    assert latest is not None
    missing = latest.normalized_intervals[5]
    if value == "missing":
        latest.normalized_intervals.remove(missing)
    else:
        missing["power_kw"] = None if value == "null" else 0
    stored_again(latest)
    today = cast(list[dict[str, Any]], saved_report_context(delivery)["days"])[0]
    daylight = today["normal"]["daylight"]
    assert daylight["partial_energy_mwh"] == pytest.approx(234)
    assert daylight["energy_mwh"] == (pytest.approx(234) if value == "zero" else None)
    assert daylight["complete"] is (value == "zero")
    hour = next(
        row for row in today["normal"]["report_intervals"] if row["start"] == missing["start"]
    )
    assert hour["partial_energy_mwh"] == pytest.approx(13)
    assert hour["coverage_fraction"] == (1 if value == "zero" else 0.5)


def test_today_report_without_archived_morning_remains_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, delivery, _ = intraday_delivery(monkeypatch, archived_morning=False)
    today = cast(list[dict[str, Any]], saved_report_context(delivery)["days"])[0]
    assert today["normal"]["energy_mwh"] is None
    assert today["normal"]["partial_energy_mwh"] == pytest.approx(377)
    assert today["normal"]["daylight"]["energy_mwh"] is None
    assert today["normal"]["daylight"]["partial_energy_mwh"] == pytest.approx(221)
    assert today["normal"]["daylight"]["complete"] is False


@pytest.mark.parametrize(
    "reason", ["publication", "availability", "issue", "acquisition", "binding", "actuals"]
)
def test_today_report_excludes_ineligible_archived_forecasts(
    monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    system, _, delivery, old = intraday_delivery(monkeypatch)
    assert old is not None
    later = SLOT + timedelta(seconds=1)
    if reason == "publication":
        ForecastResult.objects.filter(snapshot=old).update(published_at=later)
    elif reason == "availability":
        stored_again(ForecastSnapshot.objects.get(pk=old.pk), available_at=later)
    elif reason == "issue":
        stored_again(ForecastSnapshot.objects.get(pk=old.pk), issued_at=later)
    elif reason == "acquisition":
        stored_again(ForecastSnapshot.objects.get(pk=old.pk), collected_at=later)
    elif reason == "binding":
        region = Region.objects.create(
            tenant=Tenant.objects.create(name="Other tenant"),
            name="Other region",
            latitude=40,
            longitude=29,
            timezone=system.binding.timezone,
        )
        binding = SiteBinding.objects.create(
            region=region,
            provider_account=system.account,
            region_revision=1,
            site_id="other",
            capacity_kw=1,
            latitude=40,
            longitude=29,
            timezone=system.binding.timezone,
        )
        job = CollectionJob.objects.create(
            binding=binding,
            target_date=old.job.target_date,
            scheduled_for=old.job.scheduled_for,
            available_at=old.collected_at,
            status="succeeded",
        )
        stored_again(ForecastSnapshot.objects.get(pk=old.pk), binding=binding, job=job)
    else:
        stored_again(ForecastSnapshot.objects.get(pk=old.pk), source_kind="estimated_actuals")
    today = cast(list[dict[str, Any]], saved_report_context(delivery)["days"])[0]
    assert today["normal"]["daylight"]["energy_mwh"] is None
    assert today["normal"]["daylight"]["partial_energy_mwh"] == pytest.approx(221)
    assert old.pk not in today["normal"]["provenance"]["source_snapshot_ids"]


def test_prepared_intraday_report_freezes_message_and_attachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plant, delivery, old = intraday_delivery(monkeypatch)
    assert old is not None
    assert prepare_delivery(delivery, SLOT)
    assert delivery.prepared_message is not None
    assert delivery.snapshot_id is not None
    assert plant.current_configuration_id is not None
    original = deepcopy(delivery.prepared_message)
    today = cast(dict[str, Any], original["report_context"])["days"][0]
    assert today["normal"]["daylight"]["energy_mwh"] == pytest.approx(247)
    assert today["normal"]["provenance"]["source_snapshot_ids"] == [delivery.snapshot_id, old.pk]
    for source in ForecastSnapshot.objects.filter(pk__in=(old.pk, delivery.snapshot_id)):
        stored_again(source, normalized_intervals=[])
    ForecastResult.objects.filter(snapshot_id=delivery.snapshot_id).update(daily_payloads={})
    PlantConfiguration.objects.filter(pk=plant.current_configuration_id).update(dc_capacity_mw=260)
    assert prepare_delivery(delivery, SLOT + timedelta(minutes=2))
    assert delivery.prepared_message == original
    assert process_notification(delivery.pk, SLOT + timedelta(minutes=3))
    assert mail.outbox[0].body == original["body"]
    assert mail.outbox[0].attachments[0][1] == b64decode(
        cast(str, original["attachment_base64"]), validate=True
    )


def test_region_only_preparation_composes_today_and_keeps_selected_tomorrow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, delivery = region_delivery(monkeypatch)
    old = intraday_sources(system, delivery)
    assert old is not None
    assert prepare_delivery(delivery, SLOT)
    assert delivery.prepared_message is not None
    context = cast(dict[str, Any], delivery.prepared_message["report_context"])
    assert context["plant_id"] is None
    today, tomorrow = cast(list[dict[str, Any]], delivery.prepared_message["results"])
    assert today["energy_kwh_m2"] == pytest.approx(3.85)
    assert today["daylight"]["energy_kwh_m2"] == pytest.approx(1.9)
    assert today["provenance"]["source_snapshot_ids"] == [delivery.snapshot_id, old.pk]
    assert tomorrow["energy_kwh_m2"] == pytest.approx(4.8)
    assert process_notification(delivery.pk, SLOT)
    assert "kWh" in mail.outbox[0].body


@pytest.mark.parametrize("unavailable", ["missing_snapshot", "damaged_source"])
def test_region_results_leave_unavailable_inputs_missing(
    monkeypatch: pytest.MonkeyPatch, unavailable: str
) -> None:
    _, _, delivery, _ = intraday_delivery(monkeypatch)
    if unavailable == "missing_snapshot":
        delivery.snapshot = None
        CollectionJob.objects.filter(endpoint="forecasts").update(status="failed")
    else:
        assert delivery.snapshot is not None
        stored_again(
            ForecastSnapshot.objects.get(pk=delivery.snapshot.pk),
            normalized_intervals=[{"invalid": True}],
        )
    results = saved_report_results(delivery, SLOT)
    assert results[0] is None
    if unavailable == "missing_snapshot":
        assert results[1] is None
    else:
        assert results[1] is not None


def test_a_calculation_defect_is_not_left_out_as_a_missing_region_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, delivery, _ = intraday_delivery(monkeypatch)

    def defect(*args: object, **kwargs: object) -> dict[str, object]:
        raise KeyError("energy_kwh_m2")

    monkeypatch.setattr("pyracast.notifications.sources.calculate", defect)
    with pytest.raises(KeyError, match="energy_kwh_m2"):
        saved_report_results(delivery, SLOT)


def saved_intraday_actuals(
    system: System, cutoff: datetime, *, power_kw: float = 0.3
) -> ForecastSnapshot:
    day = cutoff.astimezone(ZoneInfo(system.binding.timezone)).date()
    snapshot = snapshot_for(
        system,
        day,
        power_kw=power_kw,
        collected_at=cutoff - timedelta(seconds=1),
        source_kind="estimated_actuals",
    )
    snapshot.normalized_intervals = [
        row
        for row in snapshot.normalized_intervals
        if datetime.fromisoformat(row["end"]) <= cutoff + timedelta(minutes=30)
    ]
    snapshot.job.endpoint = "estimated_actuals"
    snapshot.job.save(update_fields=("endpoint",))
    return publish_snapshot(snapshot)


def test_today_email_prioritizes_completed_valid_actuals_and_freezes_mixed_outage_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, delivery, old = intraday_delivery(monkeypatch)
    assert old is not None
    actuals = saved_intraday_actuals(system, SLOT)
    actuals.normalized_intervals[16]["power_kw"] = None
    actuals.normalized_intervals[18]["power_kw"] = 0
    actuals.normalized_intervals[20]["power_kw"] = 2
    actuals.normalized_intervals.pop(17)
    stored_again(actuals)
    monkeypatch.setattr(timezone, "now", lambda: SLOT - timedelta(minutes=1))
    day = SLOT.astimezone(ZoneInfo(system.binding.timezone)).date()
    start, _ = local_day_bounds(day, system.binding.timezone)
    incident(system, plant, start, None)
    counts = (RequestAttempt.objects.count(), CollectionJob.objects.count())
    assert prepare_delivery(delivery, SLOT)
    assert delivery.prepared_message is not None
    original = deepcopy(delivery.prepared_message)
    today, tomorrow = cast(dict[str, Any], original["report_context"])["days"]
    assert today["normal"]["energy_mwh"] == pytest.approx(708.5)
    assert today["normal"]["daylight"]["energy_mwh"] == pytest.approx(260)
    assert today["outage"]["daylight"]["energy_mwh"] == pytest.approx(130)
    assert today["normal"]["provenance"]["source_kind"] == "mixed"
    assert set(today["normal"]["provenance"]["source_snapshot_ids"]) == {
        actuals.pk,
        old.pk,
        delivery.snapshot_id,
    }
    for key in ("normal", "outage"):
        result = today[key]
        assert sum(
            row["partial_energy_mwh"] for row in result["report_intervals"]
        ) == pytest.approx(result["daylight"]["partial_energy_mwh"])
    hours = {
        datetime.fromisoformat(row["start"]).astimezone(ZoneInfo(system.binding.timezone)).hour: row
        for row in today["normal"]["report_intervals"]
    }
    assert hours[8]["energy_mwh"] == pytest.approx(13)
    assert hours[9]["energy_mwh"] == pytest.approx(19.5)
    assert hours[10]["energy_mwh"] == pytest.approx(26)
    assert tomorrow["normal"]["energy_mwh"] == pytest.approx(624)
    stored_again(ForecastSnapshot.objects.get(pk=actuals.pk), normalized_intervals=[])
    assert prepare_delivery(delivery, SLOT + timedelta(minutes=1))
    assert delivery.prepared_message == original
    assert process_notification(delivery.pk, SLOT + timedelta(minutes=2))
    assert mail.outbox[0].body == original["body"]
    assert mail.outbox[0].attachments[0][1] == b64decode(
        cast(str, original["attachment_base64"]), validate=True
    )
    assert counts == (RequestAttempt.objects.count(), CollectionJob.objects.count())


@pytest.mark.parametrize("complete_daylight", [False, True])
def test_actuals_only_today_renders_when_tomorrow_forecast_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, complete_daylight: bool
) -> None:
    system, _, delivery, _ = intraday_delivery(monkeypatch)
    CollectionJob.objects.filter(endpoint="forecasts").update(status="failed")
    delivery.snapshot = None
    day = SLOT.astimezone(ZoneInfo(system.binding.timezone)).date()
    start, _ = local_day_bounds(day, system.binding.timezone)
    cutoff = start + timedelta(hours=20) if complete_daylight else SLOT
    delivery.scheduled_for = cutoff
    delivery.preparation_deadline = cutoff + timedelta(minutes=15)
    delivery.available_at = cutoff
    delivery.save(update_fields=("scheduled_for", "preparation_deadline", "available_at"))
    actuals = saved_intraday_actuals(system, cutoff)
    assert prepare_delivery(delivery, cutoff)
    assert delivery.snapshot_id is None
    assert delivery.prepared_message is not None
    context = cast(dict[str, Any], delivery.prepared_message["report_context"])
    today, tomorrow = context["days"]
    assert today["normal"] is not None
    daylight = today["normal"]["daylight"]
    assert daylight["partial_energy_mwh"] == pytest.approx(409.5 if complete_daylight else 97.5)
    assert daylight["energy_mwh"] == (pytest.approx(409.5) if complete_daylight else None)
    assert daylight["complete"] is complete_daylight
    assert today["normal"]["provenance"]["source_kind"] == "estimated_actuals"
    assert today["normal"]["provenance"]["source_snapshot_ids"] == [actuals.pk]
    assert tomorrow["normal"] is None
    assert delivery.forecast_complete is False
    assert delivery.prepared_message["format"] == "xlsx"


def test_same_slot_forecast_grace_never_admits_actuals_published_after_reminder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, _, delivery, _ = intraday_delivery(monkeypatch)
    latest = delivery.snapshot
    assert latest is not None
    when = SLOT + timedelta(minutes=5)
    latest.collected_at = when
    latest.available_at = when
    latest.issued_at = when
    stored_again(latest)
    latest.job.scheduled_for = SLOT
    latest.job.save(update_fields=("scheduled_for",))
    cache_saved_days(latest, when)
    actuals = saved_intraday_actuals(system, SLOT)
    actuals.collected_at = when
    actuals.available_at = when
    stored_again(actuals)
    ForecastResult.objects.filter(snapshot=actuals).update(published_at=when)
    assert prepare_delivery(delivery, when)
    assert delivery.prepared_message is not None
    today = cast(dict[str, Any], delivery.prepared_message["report_context"])["days"][0]
    assert today["normal"]["daylight"]["energy_mwh"] == pytest.approx(247)
    assert today["normal"]["provenance"]["source_kind"] == "forecast"
    assert actuals.pk not in today["normal"]["provenance"]["source_snapshot_ids"]
    assert delivery.snapshot_id == latest.pk


def test_region_today_mixes_actuals_and_archive_without_selected_tomorrow_forecast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, delivery = region_delivery(monkeypatch)
    intraday_sources(system, delivery)
    actuals = saved_intraday_actuals(system, SLOT)
    ForecastResult.objects.filter(snapshot_id=delivery.snapshot_id).update(daily_payloads={})
    assert prepare_delivery(delivery, SLOT)
    assert delivery.snapshot_id is None
    assert delivery.prepared_message is not None
    today, tomorrow = cast(list[dict[str, Any] | None], delivery.prepared_message["results"])
    assert today is not None
    assert today["energy_kwh_m2"] == pytest.approx(5.8)
    assert today["daylight"]["energy_kwh_m2"] == pytest.approx(2.35)
    assert today["provenance"]["source_kind"] == "mixed"
    assert actuals.pk in today["provenance"]["source_snapshot_ids"]
    assert tomorrow is None
    assert delivery.forecast_complete is False
    assert delivery.prepared_message["format"] == "xlsx"


@pytest.mark.parametrize("publication", ["within_grace", "after_deadline"])
def test_paired_actual_collection_waits_then_uses_only_native_intervals_complete_at_reminder(
    monkeypatch: pytest.MonkeyPatch, publication: str
) -> None:
    system, _, delivery, _ = intraday_delivery(monkeypatch)
    latest = delivery.snapshot
    assert latest is not None
    forecast_published = SLOT + timedelta(minutes=1)
    latest.collected_at = forecast_published
    latest.available_at = forecast_published
    latest.issued_at = forecast_published
    stored_again(latest)
    latest.job.scheduled_for = SLOT
    latest.job.save(update_fields=("scheduled_for",))
    cache_saved_days(latest, forecast_published)
    day = SLOT.astimezone(ZoneInfo(system.binding.timezone)).date()
    start, _ = local_day_bounds(day, system.binding.timezone)
    actual_job = CollectionJob.objects.create(
        binding=system.binding,
        target_date=day,
        scheduled_for=SLOT,
        available_at=SLOT,
        endpoint="estimated_actuals",
        status="pending",
    )
    assert not prepare_delivery(delivery, SLOT + timedelta(minutes=2))
    delivery.refresh_from_db()
    assert delivery.prepared_message is None
    assert delivery.attempts == 0
    assert delivery.available_at == SLOT + timedelta(minutes=2, seconds=30)
    deadline = delivery.preparation_deadline
    assert deadline == SLOT + timedelta(minutes=15)
    assert deadline is not None
    when = SLOT + timedelta(minutes=7) if publication == "within_grace" else deadline
    if publication == "after_deadline":
        assert prepare_delivery(delivery, deadline)
        delivery = NotificationDelivery.objects.get(pk=delivery.pk)
        assert delivery.prepared_message is not None
        frozen = deepcopy(delivery.prepared_message)
        when += timedelta(seconds=1)
    actual_job.status = "succeeded"
    actual_job.save(update_fields=("status",))
    actuals = ForecastSnapshot.objects.create(
        job=actual_job,
        binding=system.binding,
        source_kind="estimated_actuals",
        raw_payload={},
        collected_at=when,
        available_at=when,
        issued_at=when,
        coverage_start=start + timedelta(hours=8),
        coverage_end=start + timedelta(hours=10, minutes=30),
        normalized_intervals=[
            {
                "start": (start + timedelta(hours=8)).isoformat(),
                "end": (start + timedelta(hours=9)).isoformat(),
                "power_kw": 0.4,
            },
            {
                "start": (start + timedelta(hours=9)).isoformat(),
                "end": SLOT.isoformat(),
                "original_start": (start + timedelta(hours=9)).isoformat(),
                "original_end": (start + timedelta(hours=10, minutes=30)).isoformat(),
                "power_kw": 2,
            },
            {
                "start": SLOT.isoformat(),
                "end": (start + timedelta(hours=10, minutes=30)).isoformat(),
                "power_kw": 2,
            },
        ],
    )
    assert prepare_delivery(delivery, when)
    delivery = NotificationDelivery.objects.get(pk=delivery.pk)
    assert delivery.prepared_message is not None
    today, tomorrow = cast(dict[str, Any], delivery.prepared_message["report_context"])["days"]
    normal = today["normal"]
    assert normal["daylight"]["energy_mwh"] == pytest.approx(
        286 if publication == "within_grace" else 247
    )
    hours = {
        datetime.fromisoformat(row["start"]).astimezone(ZoneInfo(system.binding.timezone)).hour: row
        for row in normal["intervals"]
    }
    assert hours[8]["energy_mwh"] == pytest.approx(52 if publication == "within_grace" else 13)
    assert hours[9]["energy_mwh"] == pytest.approx(19.5)
    assert hours[10]["energy_mwh"] == pytest.approx(26)
    assert actuals.pk not in hours[9]["source_snapshot_ids"]
    assert actuals.pk not in hours[10]["source_snapshot_ids"]
    assert tomorrow["normal"]["energy_mwh"] == pytest.approx(624)
    assert delivery.snapshot_id == latest.pk
    if publication == "within_grace":
        assert normal["provenance"]["source_kind"] == "mixed"
        assert normal["provenance"]["elapsed_cutoff_at"] == SLOT.isoformat()
        assert actuals.pk in hours[8]["source_snapshot_ids"]
    else:
        assert delivery.prepared_message == frozen
        assert normal["provenance"]["source_kind"] == "forecast"
