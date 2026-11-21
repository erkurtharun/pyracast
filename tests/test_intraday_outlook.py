from datetime import date, datetime, timedelta
from typing import Any, cast

import pytest
from django.test import Client
from django.utils import timezone

from pyracast.accounts.models import Tenant
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.history import regional_outlook
from pyracast.forecasts.intraday import (
    INTRADAY_POLICY,
    apply_intraday_source,
    prepare_intraday_source,
)
from pyracast.forecasts.models import CollectionJob, ForecastSnapshot, RequestAttempt
from pyracast.incidents.operations import save_incident
from pyracast.irradiance import local_day_bounds
from pyracast.plants.forecast_sources import replay_composition
from pyracast.plants.forecasting import forecast_payload
from pyracast.plants.history import plant_outlook
from pyracast.plants.models import CalibrationActivation
from pyracast.plants.transfer import transfer_model
from pyracast.regions.models import Region
from pyracast.solar import daylight_window
from pyracast.weather_data import serialize_weather
from tests.support.domain import System
from tests.support.errors import raises_code
from tests.support.invariants import stored_again
from tests.support.plant_operations import make_plant, publish_snapshot, ready_run, snapshot_for
from tests.support.weather_collection import WeatherFixture

pytestmark = pytest.mark.django_db(transaction=True)
DAY = date(2026, 11, 11)


def intraday_actuals(system: System, when: datetime, *, power_kw: float = 0.4) -> ForecastSnapshot:
    snapshot = snapshot_for(
        system, DAY, power_kw=power_kw, collected_at=when, source_kind="estimated_actuals"
    )
    snapshot.job.endpoint = "estimated_actuals"
    snapshot.job.save(update_fields=("endpoint",))
    return publish_snapshot(snapshot)


def test_today_auto_combines_completed_actuals_with_forecast_without_provider_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    now = start + timedelta(hours=12, minutes=15)
    forecast = publish_snapshot(snapshot_for(system, DAY, power_kw=0.2))
    actual = intraday_actuals(system, now - timedelta(minutes=1))
    monkeypatch.setattr(timezone, "now", lambda: now)
    requests = RequestAttempt.objects.count()
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    outlook = plant_outlook(*scope, target_date=DAY, resolution_minutes=60, source="auto")
    result = cast(dict[str, Any], outlook["result"])
    assert outlook["source_kind"] == "mixed"
    assert result["energy_mwh"] == pytest.approx(936)
    assert result["intervals"][11]["mean_power_mw"] == pytest.approx(52)
    assert result["intervals"][12]["mean_power_mw"] == pytest.approx(26)
    assert result["intervals"][11]["source_kinds"] == ["estimated_actuals"]
    assert result["intervals"][12]["source_kinds"] == ["forecast"]
    assert result["provenance"]["source_snapshot_ids"] == [actual.pk, forecast.pk]
    assert result["provenance"]["selection_policy"] == INTRADAY_POLICY
    explicit = plant_outlook(*scope, target_date=DAY, resolution_minutes=60, source="forecast")
    assert explicit["source_kind"] == "forecast"
    assert cast(dict[str, Any], explicit["result"])["energy_mwh"] == pytest.approx(624)
    system.region.refresh_from_db()
    regional = regional_outlook(system.region, DAY, "auto", now)
    assert regional["source_kind"] == "mixed"
    assert cast(dict[str, Any], regional["result"])["intervals"][0]["source_kinds"] == [
        "estimated_actuals"
    ]
    client = Client()
    client.force_login(system.member)
    response = client.get(
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/forecast/",
        {"date": DAY.isoformat(), "data_source": "auto", "resolution_minutes": "60"},
    )
    assert response.status_code == 200, response.content
    assert response.json()["source_kind"] == "mixed"
    assert RequestAttempt.objects.count() == requests


def test_actual_zero_wins_but_missing_negative_and_unfinished_actuals_keep_forecast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    now = start + timedelta(hours=12, minutes=15)
    publish_snapshot(snapshot_for(system, DAY, power_kw=0.2))
    actual = intraday_actuals(system, now - timedelta(minutes=1))
    actual.normalized_intervals[20]["power_kw"] = 0
    actual.normalized_intervals[21]["power_kw"] = None
    actual.normalized_intervals[22]["power_kw"] = -1
    actual.normalized_intervals[19]["power_kw"] = True
    del actual.normalized_intervals[23]
    stored_again(actual)
    monkeypatch.setattr(timezone, "now", lambda: now)
    result = cast(
        dict[str, Any],
        plant_outlook(
            system.member,
            system.tenant.pk,
            system.region.pk,
            plant.pk,
            target_date=DAY,
            resolution_minutes=30,
            source="auto",
        )["result"],
    )
    assert result["intervals"][20]["mean_power_mw"] == 0
    assert result["intervals"][20]["source_kinds"] == ["estimated_actuals"]
    for index in (19, 21, 22, 23, 24):
        assert result["intervals"][index]["mean_power_mw"] == pytest.approx(26)
        assert result["intervals"][index]["source_kinds"] == ["forecast"]


@pytest.mark.parametrize("ineligible", ["collected", "available", "issued", "failed", "endpoint"])
def test_current_actuals_respect_knowledge_and_native_source_boundaries(
    monkeypatch: pytest.MonkeyPatch, ineligible: str
) -> None:
    system, _ = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    now = start + timedelta(hours=12)
    forecast = publish_snapshot(snapshot_for(system, DAY))
    actual = intraday_actuals(system, now - timedelta(minutes=1))
    if ineligible in {"collected", "available", "issued"}:
        setattr(actual, f"{ineligible}_at", now + timedelta(seconds=1))
        stored_again(actual)
    else:
        if ineligible == "failed":
            actual.job.status = "failed"
        else:
            actual.job.endpoint = "forecasts"
        actual.job.save()
    prepared = prepare_intraday_source(
        system.binding,
        DAY,
        [forecast],
        knowledge_cutoff=now,
        elapsed_cutoff=now,
    )
    assert prepared is not None
    assert prepared.source_kind == "forecast"
    assert prepared.composition.snapshot_ids == (forecast.pk,)


def test_explicit_today_actuals_and_auto_complete_daylight_need_no_forecast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    now = start + timedelta(hours=20)
    actual = intraday_actuals(system, now - timedelta(minutes=1))
    monkeypatch.setattr(timezone, "now", lambda: now)
    scope = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    for source in ("auto", "estimated_actuals"):
        outlook = plant_outlook(*scope, target_date=DAY, resolution_minutes=60, source=source)
        result = cast(dict[str, Any], outlook["result"])
        assert outlook["source_kind"] == "estimated_actuals"
        assert result["energy_mwh"] is None
        assert result["daylight"]["complete"] is True
        assert result["provenance"]["source_snapshot_ids"] == [actual.pk]
        assert result["intervals"][20]["mean_power_mw"] is None
        daylight = daylight_window(DAY, system.binding.timezone, 40, 29)
        assert daylight.sunrise is not None
        assert daylight.sunset is not None
        first = daylight.sunrise.replace(
            minute=daylight.sunrise.minute // 30 * 30, second=0, microsecond=0
        )
        last = daylight.sunset.replace(
            minute=daylight.sunset.minute // 30 * 30, second=0, microsecond=0
        ) + timedelta(minutes=30)
        assert result["daylight"]["energy_mwh"] == pytest.approx(
            52 * (last - first).total_seconds() / 3600
        )
    system.region.refresh_from_db()
    regional = regional_outlook(system.region, DAY, "estimated_actuals", now)
    assert regional["source_kind"] == "estimated_actuals"


def test_outage_replays_pinned_mixed_sources_and_invalidated_calibration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    now = start + timedelta(hours=12, minutes=15)
    forecast = publish_snapshot(snapshot_for(system, DAY, power_kw=0.2))
    actual = intraday_actuals(system, now - timedelta(minutes=1))
    run = ready_run(system, plant)
    assert plant.current_configuration is not None
    CalibrationActivation.objects.create(
        plant=plant, configuration=plant.current_configuration, calibration=run
    )
    monkeypatch.setattr(timezone, "now", lambda: now)
    save_incident(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        kind="panels",
        title="Reduced array",
        notes="",
        starts_at=start + timedelta(hours=10),
        ends_at=None,
        available_dc_mw=65,
        available_inverter_mw=None,
        available_export_mw=None,
    )
    monkeypatch.setattr("pyracast.plants.incident_forecasts.incidents_unchanged", lambda *_: False)
    outlook = plant_outlook(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
        source="auto",
    )
    normal = cast(dict[str, Any], outlook["result"])
    scenario = cast(dict[str, Any], outlook["outage_scenario"])["result"]
    assert normal["factor"] == 1
    assert normal["energy_mwh"] == pytest.approx(936)
    assert scenario["energy_mwh"] == pytest.approx(728)
    assert scenario["intervals"][10]["source_kinds"] == ["estimated_actuals"]
    assert scenario["intervals"][13]["source_kinds"] == ["forecast"]
    newer = intraday_actuals(system, now + timedelta(hours=1), power_kw=0.8)
    assert plant.current_configuration is not None
    replay = replay_composition(
        normal["provenance"], plant.current_configuration, start, start + timedelta(days=1)
    )
    assert replay is not None
    assert replay[0].snapshot_ids == (actual.pk, forecast.pk)
    assert newer.pk not in replay[0].snapshot_ids
    assert len(replay[0].intervals) == 48


def test_latest_actual_vintage_retains_its_gaps_instead_of_older_complete_actuals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, _ = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    now = start + timedelta(hours=20)
    forecast = publish_snapshot(snapshot_for(system, DAY, power_kw=0.2))
    old = intraday_actuals(system, now - timedelta(hours=1))
    latest = intraday_actuals(system, now - timedelta(minutes=1), power_kw=0.6)
    latest.normalized_intervals = latest.normalized_intervals[:16]
    stored_again(latest)
    prepared = prepare_intraday_source(
        system.binding,
        DAY,
        [forecast],
        knowledge_cutoff=now,
        elapsed_cutoff=now,
    )
    assert prepared is not None
    assert prepared.source_kind == "mixed"
    assert prepared.composition.snapshot_ids == (latest.pk, forecast.pk)
    assert old.pk not in prepared.composition.snapshot_ids
    noon = start + timedelta(hours=12)
    row = next(
        interval
        for interval in prepared.composition.intervals
        if datetime.fromisoformat(interval["start"]) == noon
    )
    assert row["source_snapshot_id"] == forecast.pk
    assert row["power_kw"] == pytest.approx(0.2)


@pytest.mark.parametrize(("day", "hours"), [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)])
def test_intraday_actual_ownership_preserves_elapsed_dst_intervals(
    monkeypatch: pytest.MonkeyPatch, day: date, hours: int
) -> None:
    system, _ = make_plant(monkeypatch)
    system.binding.timezone = "Europe/London"
    system.binding.save(update_fields=("timezone",))
    start, end = local_day_bounds(day, system.binding.timezone)
    now = start + timedelta(hours=12, minutes=15)
    forecast = publish_snapshot(snapshot_for(system, day, power_kw=0.2))
    actual = snapshot_for(
        system, day, power_kw=0.4, collected_at=now, source_kind="estimated_actuals"
    )
    actual.job.endpoint = "estimated_actuals"
    actual.job.save(update_fields=("endpoint",))
    publish_snapshot(actual)
    prepared = prepare_intraday_source(
        system.binding, day, [forecast], knowledge_cutoff=now, elapsed_cutoff=now
    )
    assert prepared is not None
    assert prepared.source_kind == "mixed"
    rows = prepared.composition.intervals
    assert len(rows) == hours * 2
    assert (
        sum(
            (
                datetime.fromisoformat(row["end"]) - datetime.fromisoformat(row["start"])
            ).total_seconds()
            for row in rows
        )
        == (end - start).total_seconds()
    )
    assert sum(row["source_snapshot_id"] == actual.pk for row in rows) == 24
    assert sum(row["source_snapshot_id"] == forecast.pk for row in rows) == hours * 2 - 24


def test_current_day_actuals_cannot_cross_a_shared_provider_accounts_tenant_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, _ = make_plant(monkeypatch)
    start, end = local_day_bounds(DAY, system.binding.timezone)
    now = start + timedelta(hours=12)
    forecast = publish_snapshot(snapshot_for(system, DAY))
    other = SiteBinding.objects.create(
        region=Region.objects.create(
            tenant=Tenant.objects.create(name="Other"),
            name="Other plant",
            latitude=40,
            longitude=29,
            timezone=system.binding.timezone,
        ),
        provider_account=system.account,
        region_revision=1,
        site_id="other",
        capacity_kw=1,
        latitude=40,
        longitude=29,
        timezone=system.binding.timezone,
    )
    job = CollectionJob.objects.create(
        binding=other,
        target_date=DAY,
        scheduled_for=now,
        available_at=now,
        endpoint="estimated_actuals",
        status="succeeded",
    )
    foreign = ForecastSnapshot.objects.create(
        job=job,
        binding=other,
        raw_payload={},
        normalized_intervals=forecast.normalized_intervals,
        source_kind="estimated_actuals",
        collected_at=now,
        available_at=now,
        coverage_start=start,
        coverage_end=end,
    )
    prepared = prepare_intraday_source(
        system.binding, DAY, [forecast], knowledge_cutoff=now, elapsed_cutoff=now
    )
    assert prepared is not None
    assert prepared.source_kind == "forecast"
    assert prepared.composition.snapshot_ids == (forecast.pk,)
    assert foreign.pk not in prepared.composition.snapshot_ids


@pytest.mark.parametrize("exact_slot", [False, True])
def test_paired_actuals_grace_admits_only_its_slot_and_completed_native_intervals(
    monkeypatch: pytest.MonkeyPatch, exact_slot: bool
) -> None:
    system, plant = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    slot = start + timedelta(hours=12, minutes=15)
    grace = slot + timedelta(minutes=10)
    forecast = publish_snapshot(snapshot_for(system, DAY))
    actual = intraday_actuals(system, grace)
    actual.job.scheduled_for = slot if exact_slot else slot + timedelta(seconds=1)
    actual.job.save(update_fields=("scheduled_for",))
    prepared = prepare_intraday_source(
        system.binding,
        DAY,
        [forecast],
        knowledge_cutoff=slot,
        elapsed_cutoff=slot,
        actuals_grace_until=grace,
    )
    assert prepared is not None
    assert prepared.source_kind == ("mixed" if exact_slot else "forecast")
    actual_rows = [
        row for row in prepared.composition.intervals if row["source_snapshot_id"] == actual.pk
    ]
    assert len(actual_rows) == (24 if exact_slot else 0)
    assert all(datetime.fromisoformat(row["end"]) <= slot for row in actual_rows)
    assert plant.current_configuration is not None
    payload = forecast_payload(
        plant.current_configuration,
        prepared.snapshot,
        None,
        DAY,
        60,
        model_version=transfer_model(plant.current_configuration, None),
        source_intervals=prepared.composition.intervals,
    )
    apply_intraday_source(payload, prepared)
    provenance = cast(dict[str, Any], payload["provenance"])
    if exact_slot:
        assert provenance["actuals_cutoff_at"] == slot.isoformat()
        assert provenance["actuals_grace_until"] == grace.isoformat()
    without_grace = prepare_intraday_source(
        system.binding,
        DAY,
        [forecast],
        knowledge_cutoff=slot,
        elapsed_cutoff=slot,
    )
    assert without_grace is not None
    assert without_grace.source_kind == "forecast"


def test_acquisition_clipping_does_not_turn_an_unfinished_native_actual_into_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, _ = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    now = start + timedelta(hours=10, minutes=15)
    forecast = publish_snapshot(snapshot_for(system, DAY, power_kw=0.2))
    actual = intraday_actuals(system, now)
    row = actual.normalized_intervals[20]
    row.update(original_start=row["start"], original_end=row["end"], end=now.isoformat())
    actual.normalized_intervals = actual.normalized_intervals[:21]
    actual.coverage_end = now
    stored_again(actual)
    prepared = prepare_intraday_source(
        system.binding, DAY, [forecast], knowledge_cutoff=now, elapsed_cutoff=now
    )
    assert prepared is not None
    at_ten = next(
        item
        for item in prepared.composition.intervals
        if datetime.fromisoformat(item["start"]) == start + timedelta(hours=10)
    )
    assert at_ten["source_snapshot_id"] == forecast.pk
    assert at_ten["power_kw"] == pytest.approx(0.2)


def test_mixed_native_means_are_limited_before_hourly_aggregation_and_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    start, end = local_day_bounds(DAY, system.binding.timezone)
    now = start + timedelta(hours=12)
    publish_snapshot(snapshot_for(system, DAY, power_kw=2))
    actual = intraday_actuals(system, now)
    actual.normalized_intervals = [
        {
            "start": (start + timedelta(hours=10, minutes=15)).isoformat(),
            "end": (start + timedelta(hours=10, minutes=45)).isoformat(),
            "power_kw": 0.4,
        }
    ]
    stored_again(actual)
    monkeypatch.setattr(timezone, "now", lambda: now)
    outlook = plant_outlook(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
        source="auto",
    )
    result = cast(dict[str, Any], outlook["result"])
    assert result["intervals"][10]["mean_power_mw"] == pytest.approx(76)
    assert result["energy_mwh"] == pytest.approx(2376)
    assert result["intervals"][10]["source_kinds"] == ["estimated_actuals", "forecast"]
    assert "uniform_mean_proration" in result["quality_flags"]
    assert plant.current_configuration is not None
    replayed = replay_composition(result["provenance"], plant.current_configuration, start, end)
    assert replayed is not None
    assert replayed[0].intervals[0]["source_snapshot_id"] != actual.pk
    assert any("original_start" in row for row in replayed[0].intervals)


@pytest.mark.parametrize("weather", ["available", "failed", "absent"])
def test_today_actuals_keep_independent_archived_forecast_weather(
    monkeypatch: pytest.MonkeyPatch, weather: str
) -> None:
    system, plant = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    now = start + timedelta(hours=12)
    forecast = publish_snapshot(snapshot_for(system, DAY, power_kw=0.2))
    if weather == "available":
        forecast.weather_payload = serialize_weather(
            WeatherFixture().fetch(40, 29, start, start + timedelta(days=1))
        )
    elif weather == "failed":
        forecast.weather_error = "weather_request_failed"
    stored_again(forecast)
    intraday_actuals(system, now)
    monkeypatch.setattr(timezone, "now", lambda: now)
    outlook = plant_outlook(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
        source="auto",
    )
    regional = cast(dict[str, Any], outlook["regional_irradiance"])
    assert regional["vintage"]["scope"] == "primary_source"
    if weather == "available":
        assert regional["weather_reference"]["energy_kwh_m2"] == pytest.approx(14.4)
        assert regional["weather_reference"]["intervals"][0]["ghi_wm2"] == 600
    elif weather == "failed":
        assert regional["weather_reference"]["status"] == "unavailable"
        assert "weather_request_failed" in regional["weather_reference"]["quality_flags"]
    else:
        assert "weather_reference" not in regional


@pytest.mark.parametrize("before_configuration", [False, True])
def test_intraday_actual_configuration_assumption_follows_contributing_native_intervals(
    monkeypatch: pytest.MonkeyPatch, before_configuration: bool
) -> None:
    system, plant = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    now = start + timedelta(hours=12)
    assert plant.current_configuration is not None
    configuration = plant.current_configuration
    configuration.created_at = start + timedelta(hours=10)
    configuration.save(update_fields=("created_at",))
    actual = intraday_actuals(system, now)
    actual.normalized_intervals = actual.normalized_intervals[
        18 if before_configuration else 20 : 24
    ]
    stored_again(actual)
    monkeypatch.setattr(timezone, "now", lambda: now)
    outlook = plant_outlook(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
        source="estimated_actuals",
    )
    result = cast(dict[str, Any], outlook["result"])
    assert ("historical_configuration_assumed" in result["quality_flags"]) is before_configuration


@pytest.mark.parametrize("complete_daylight", [False, True])
def test_disjoint_actual_acquisitions_keep_owned_history_and_can_complete_daylight(
    monkeypatch: pytest.MonkeyPatch, complete_daylight: bool
) -> None:
    system, plant = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    now = start + timedelta(hours=20 if complete_daylight else 12)
    forecast = publish_snapshot(snapshot_for(system, DAY, power_kw=0.2))
    old = intraday_actuals(system, now - timedelta(hours=1), power_kw=0.3)
    new = intraday_actuals(system, now, power_kw=0.4)
    old.normalized_intervals = (
        old.normalized_intervals[:24] if complete_daylight else old.normalized_intervals[14:18]
    )
    new.normalized_intervals = (
        new.normalized_intervals[24:40] if complete_daylight else new.normalized_intervals[18:20]
    )
    for snapshot in (old, new):
        snapshot.coverage_start = datetime.fromisoformat(snapshot.normalized_intervals[0]["start"])
        snapshot.coverage_end = datetime.fromisoformat(snapshot.normalized_intervals[-1]["end"])
        stored_again(snapshot)
    prepared = prepare_intraday_source(
        system.binding, DAY, [forecast], knowledge_cutoff=now, elapsed_cutoff=now
    )
    assert prepared is not None
    assert prepared.source_kind == ("estimated_actuals" if complete_daylight else "mixed")
    assert old.pk in prepared.composition.snapshot_ids
    assert new.pk in prepared.composition.snapshot_ids
    assert (forecast.pk in prepared.composition.snapshot_ids) is not complete_daylight
    assert plant.current_configuration is not None
    result = forecast_payload(
        plant.current_configuration,
        prepared.snapshot,
        None,
        DAY,
        60,
        model_version=transfer_model(plant.current_configuration, None),
        source_intervals=prepared.composition.intervals,
    )
    apply_intraday_source(result, prepared)
    assert "mixed_estimated_actual_vintages" in cast(list[str], result["quality_flags"])
    before = start + timedelta(hours=8)
    after = start + timedelta(hours=13 if complete_daylight else 9)
    for point, identifier in ((before, old.pk), (after, new.pk)):
        row = next(
            interval
            for interval in prepared.composition.intervals
            if datetime.fromisoformat(interval["start"]) == point
        )
        assert row["source_snapshot_id"] == identifier


def test_complete_astronomical_actuals_need_the_whole_native_sunrise_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, _ = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    now = start + timedelta(hours=20)
    forecast = publish_snapshot(snapshot_for(system, DAY, power_kw=0.2))
    actual = intraday_actuals(system, now)
    daylight = daylight_window(DAY, system.binding.timezone, 40, 29)
    assert daylight.sunrise is not None
    first = next(
        index
        for index, row in enumerate(actual.normalized_intervals)
        if datetime.fromisoformat(row["end"]) > daylight.sunrise
    )
    actual.normalized_intervals = actual.normalized_intervals[first:]
    row = actual.normalized_intervals[0]
    native_start = datetime.fromisoformat(row["start"])
    partial_start = native_start + (daylight.sunrise - native_start) / 2
    row.update(
        original_start=row["start"], original_end=row["end"], start=partial_start.isoformat()
    )
    actual.coverage_start = partial_start
    stored_again(actual)
    prepared = prepare_intraday_source(
        system.binding, DAY, [forecast], knowledge_cutoff=now, elapsed_cutoff=now
    )
    assert prepared is not None
    assert prepared.source_kind == "mixed"
    assert forecast.pk in prepared.composition.snapshot_ids
    assert partial_start < daylight.sunrise


def test_mixed_replay_preserves_a_calendar_clipped_forecasts_original_native_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    system.binding.timezone = "Asia/Kathmandu"
    system.binding.save(update_fields=("timezone",))
    Region.objects.filter(pk=system.region.pk).update(timezone="Asia/Kathmandu")
    start, end = local_day_bounds(DAY, system.binding.timezone)
    now = start + timedelta(hours=12)
    forecast = publish_snapshot(snapshot_for(system, DAY, power_kw=0.2))
    row = forecast.normalized_intervals[0]
    row.update(
        original_start=(start - timedelta(minutes=15)).isoformat(),
        original_end=(start + timedelta(minutes=15)).isoformat(),
        end=(start + timedelta(minutes=15)).isoformat(),
    )
    stored_again(forecast)
    actual = intraday_actuals(system, now)
    actual.normalized_intervals = actual.normalized_intervals[14:24]
    stored_again(actual)
    monkeypatch.setattr(timezone, "now", lambda: now)
    outlook = plant_outlook(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
        source="auto",
    )
    result = cast(dict[str, Any], outlook["result"])
    assert outlook["source_kind"] == "mixed"
    assert plant.current_configuration is not None
    replayed = replay_composition(result["provenance"], plant.current_configuration, start, end)
    assert replayed is not None
    first = replayed[0].intervals[0]
    assert first["start"] == start.isoformat()
    assert first["end"] == (start + timedelta(minutes=15)).isoformat()
    assert first["original_start"] == (start - timedelta(minutes=15)).isoformat()
    assert first["original_end"] == (start + timedelta(minutes=15)).isoformat()


@pytest.mark.parametrize("invalid", ["naive_start", "naive_end", "outside_native"])
def test_intraday_rejects_damaged_original_native_bounds(
    monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    system, plant = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    now = start + timedelta(hours=12)
    actual = intraday_actuals(system, now)
    row = actual.normalized_intervals[20]
    if invalid == "naive_start":
        row["original_start"] = (
            datetime.fromisoformat(row["start"]).replace(tzinfo=None).isoformat()
        )
    elif invalid == "naive_end":
        row["original_end"] = datetime.fromisoformat(row["end"]).replace(tzinfo=None).isoformat()
    else:
        row["original_end"] = row["start"]
    stored_again(actual)
    monkeypatch.setattr(timezone, "now", lambda: now)
    with raises_code("calculation_out_of_range"):
        plant_outlook(
            system.member,
            system.tenant.pk,
            system.region.pk,
            plant.pk,
            target_date=DAY,
            resolution_minutes=60,
            source="auto",
        )
    system.region.refresh_from_db()
    with raises_code("calculation_out_of_range"):
        regional_outlook(system.region, DAY, "auto", now)


def test_intraday_missing_inputs_stay_unavailable_and_naive_cutoffs_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    now = start + timedelta(hours=12)
    monkeypatch.setattr(timezone, "now", lambda: now)
    outlook = plant_outlook(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
        source="estimated_actuals",
    )
    assert outlook["result"] is None
    assert outlook["unavailable_reason"] == "estimated_actuals_unavailable"
    with pytest.raises(ValueError, match="timezone-aware"):
        prepare_intraday_source(
            system.binding, DAY, [], knowledge_cutoff=now.replace(tzinfo=None), elapsed_cutoff=now
        )
    wrong_product = intraday_actuals(system, now)
    with pytest.raises(ValueError, match="selected binding and product"):
        prepare_intraday_source(
            system.binding, DAY, [wrong_product], knowledge_cutoff=now, elapsed_cutoff=now
        )


@pytest.mark.parametrize("damage", ["missing_snapshot", "missing_native_interval"])
def test_pinned_intraday_replay_rejects_unavailable_source_evidence(
    monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    system, plant = make_plant(monkeypatch)
    start, end = local_day_bounds(DAY, system.binding.timezone)
    now = start + timedelta(hours=12)
    forecast = publish_snapshot(snapshot_for(system, DAY))
    actual = intraday_actuals(system, now)
    source = prepare_intraday_source(
        system.binding, DAY, [forecast], knowledge_cutoff=now, elapsed_cutoff=now
    )
    assert source is not None
    assert plant.current_configuration is not None
    result = forecast_payload(
        plant.current_configuration,
        source.snapshot,
        None,
        DAY,
        60,
        model_version=transfer_model(plant.current_configuration, None),
        source_intervals=source.composition.intervals,
    )
    apply_intraday_source(result, source)
    provenance = cast(dict[str, Any], result["provenance"])
    if damage == "missing_snapshot":
        provenance["source_snapshot_ids"].append(99999999)
    else:
        actual.normalized_intervals = []
        stored_again(actual)
    with pytest.raises(ValueError, match="unavailable"):
        replay_composition(provenance, plant.current_configuration, start, end)
