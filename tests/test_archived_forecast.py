from datetime import timedelta
from typing import Any, cast

import pytest
from django.core.exceptions import ValidationError

from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import ForecastResult
from pyracast.incidents.operations import save_incident
from pyracast.irradiance import local_day_bounds
from pyracast.plants.forecast_archive import forecast_cutoff
from pyracast.plants.forecasting import archived_forecast, plant_forecast
from pyracast.plants.history import plant_outlook
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    ForecastIssuance,
    PlantConfiguration,
    PlantForecast,
)
from pyracast.plants.operations import activate_calibration, reset_calibration
from tests.support.domain import NOW
from tests.support.errors import raises_code
from tests.support.plant_operations import make_plant, publish_snapshot, ready_run, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)
DAY = NOW.date() - timedelta(days=1)


def test_archive_replays_previous_binding_without_writing_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    old = plant.configurations.get()
    snapshot = publish_snapshot(snapshot_for(system, DAY))
    binding = SiteBinding.objects.get(pk=system.binding.pk)
    binding.pk = None
    binding.site_id = "new-site"
    binding.capacity_kw = 1000
    binding.save()
    current = PlantConfiguration.objects.create(
        region_id=plant.region_id,
        plant=plant,
        binding=binding,
        revision=old.revision + 1,
        dc_capacity_mw=130,
        created_by=system.member,
    )
    plant.current_configuration = current
    plant.save(update_fields=["current_configuration"])
    replay = archived_forecast(plant, DAY, 60, NOW)
    result = cast(dict[str, Any], replay["result"])
    assert replay["configuration_id"] == old.pk
    assert replay["snapshot_id"] == snapshot.pk
    assert result["complete"] is True
    assert result["energy_mwh"] == pytest.approx(1248)
    assert result["provenance"]["binding_id"] == old.binding_id
    assert "archived_forecast" in result["quality_flags"]
    assert "previous_configuration" in result["quality_flags"]
    assert not PlantForecast.objects.exists()
    strict = plant_forecast(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=30,
    )
    assert strict["configuration_id"] == old.pk
    assert strict["snapshot_id"] == snapshot.pk
    assert cast(dict[str, Any], strict["result"])["energy_mwh"] == pytest.approx(1248)
    assert ForecastIssuance.objects.count() == 1


@pytest.mark.parametrize("legacy", ["none", "regional", "electrical_cache"])
def test_archive_requires_a_saved_electrical_issuance(
    monkeypatch: pytest.MonkeyPatch, legacy: str
) -> None:
    system, plant = make_plant(monkeypatch)
    snapshot = snapshot_for(system, DAY)
    if legacy != "none":
        snapshot.job.status = "succeeded"
        snapshot.job.save(update_fields=["status"])
        snapshot.available_at = snapshot.collected_at
        snapshot.save(update_fields=["available_at"])
        ForecastResult.objects.create(
            snapshot=snapshot, payload={}, published_at=snapshot.collected_at
        )
    if legacy == "electrical_cache":
        configuration = plant.current_configuration
        assert configuration is not None
        PlantForecast.objects.create(
            configuration=configuration,
            snapshot=snapshot,
            calibration=None,
            target_date=DAY,
            resolution_minutes=30,
            payload={"energy_mwh": 999},
        )
    replay = archived_forecast(plant, DAY, 30, NOW)
    assert replay["result"] is None
    assert replay["unavailable_reason"] == "forecast_unavailable"
    assert not ForecastIssuance.objects.exists()
    assert PlantForecast.objects.count() == (1 if legacy == "electrical_cache" else 0)


def test_archive_does_not_backdate_a_later_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration = plant.configurations.get()
    PlantConfiguration.objects.filter(pk=configuration.pk).update(created_at=NOW)
    publish_snapshot(snapshot_for(system, DAY))
    replay = archived_forecast(plant, DAY, 30, NOW)
    assert replay["result"] is None
    assert replay["unavailable_reason"] == "configuration_unavailable"
    assert not ForecastIssuance.objects.exists()


def test_archive_rejects_future_days(monkeypatch: pytest.MonkeyPatch) -> None:
    _, plant = make_plant(monkeypatch)
    with raises_code("date_out_of_range"):
        archived_forecast(plant, NOW.date() + timedelta(days=1), 30, NOW)


def test_archive_reuses_frozen_issuance_without_changing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    publish_snapshot(snapshot_for(system, DAY))
    strict = plant_forecast(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
    )
    cached = ForecastIssuance.objects.get()
    before = cached.payloads
    replay = archived_forecast(plant, DAY, 60, NOW)
    assert (
        cast(dict[str, Any], replay["result"])["energy_mwh"]
        == cast(dict[str, Any], strict["result"])["energy_mwh"]
    )
    cached.refresh_from_db()
    assert cached.payloads == before
    assert "archived_forecast" not in cached.payloads["60"]["quality_flags"]
    assert not PlantForecast.objects.exists()


@pytest.mark.parametrize("resolution", [True, 15])
def test_archive_rejects_invalid_resolution(
    monkeypatch: pytest.MonkeyPatch, resolution: int
) -> None:
    _, plant = make_plant(monkeypatch)
    with pytest.raises(ValidationError, match="Resolution"):
        archived_forecast(plant, DAY, resolution, NOW)


@pytest.mark.parametrize("bad_cutoff", [NOW.replace(tzinfo=None), NOW + timedelta(seconds=1)])
def test_archive_rejects_invalid_cutoff(monkeypatch: pytest.MonkeyPatch, bad_cutoff: Any) -> None:
    _, plant = make_plant(monkeypatch)
    with pytest.raises(ValidationError, match="as_of"):
        archived_forecast(plant, DAY, 30, NOW, as_of=bad_cutoff)


def test_archive_uses_frozen_output_after_raw_source_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    snapshot = publish_snapshot(snapshot_for(system, DAY))
    original = archived_forecast(plant, DAY, 30, NOW)
    snapshot.normalized_intervals[0]["power_kw"] = -1
    snapshot.save(update_fields=["normalized_intervals"])
    replay = archived_forecast(plant, DAY, 30, NOW)
    assert replay["result"] == original["result"]
    assert replay["calibration_id"] == original["calibration_id"]


def test_archive_preserves_applied_calibration_after_later_outage_and_model_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    source = snapshot_for(system, DAY)
    run = ready_run(system, plant)
    activation = activate_calibration(
        system.member, system.tenant.pk, system.region.pk, plant.pk, run.pk
    )
    eligible = source.collected_at - timedelta(hours=1)
    CalibrationRun.objects.filter(pk=run.pk).update(
        created_at=eligible, cutoff_at=eligible, completed_at=eligible
    )
    CalibrationActivation.objects.filter(pk=activation.pk).update(created_at=eligible)
    publish_snapshot(source)
    args = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    original = plant_outlook(
        *args, target_date=DAY, resolution_minutes=60, source="archived_forecast"
    )
    assert cast(dict[str, Any], original["result"])["factor"] == 0.5
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    save_incident(
        *args,
        kind="panels",
        title="Late reported outage",
        notes="",
        starts_at=start + timedelta(hours=8),
        ends_at=None,
        available_dc_mw=65,
        available_inverter_mw=None,
        available_export_mw=None,
    )
    reset_calibration(*args)

    def unavailable(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Archive replay must not execute live incident policy.")

    monkeypatch.setattr("pyracast.plants.incident_forecasts.incident_scenario", unavailable)
    monkeypatch.setattr("pyracast.plants.forecasting.transfer_model", unavailable)
    archived = plant_outlook(
        *args, target_date=DAY, resolution_minutes=60, source="archived_forecast"
    )
    strict = plant_outlook(*args, target_date=DAY, resolution_minutes=60, as_of=NOW)
    assert archived == original
    assert strict == {**original, "as_of": NOW.isoformat()}
    assert archived["calibration_id"] == run.pk
    assert archived["outage_scenario"] is None


def test_archive_keeps_latest_evening_and_pre_sunrise_updates_separate_from_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    day = NOW.date()
    start, _ = local_day_bounds(day, system.binding.timezone)
    morning = publish_snapshot(
        snapshot_for(system, day, power_kw=0.1, collected_at=start - timedelta(hours=15))
    )
    evening = publish_snapshot(
        snapshot_for(system, day, power_kw=0.2, collected_at=start - timedelta(hours=7))
    )
    assert archived_forecast(plant, day, 60, NOW)["snapshot_id"] == evening.pk
    night = publish_snapshot(
        snapshot_for(system, day, power_kw=0.3, collected_at=start + timedelta(hours=3))
    )
    midday = publish_snapshot(
        snapshot_for(system, day, power_kw=0.4, collected_at=start + timedelta(hours=10))
    )
    archive = archived_forecast(plant, day, 60, NOW)
    assert archive["snapshot_id"] == night.pk
    assert archive["snapshot_id"] != morning.pk
    assert archive["as_of"] == forecast_cutoff(day, system.binding).isoformat()
    assert not ForecastIssuance.objects.filter(snapshot=midday, target_date=day).exists()
    current = plant_outlook(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=day,
        resolution_minutes=60,
        source="forecast",
    )
    assert cast(dict[str, Any], current["result"])["provenance"]["snapshot_id"] == midday.pk
    assert (
        cast(dict[str, Any], current["result"])["energy_mwh"]
        != cast(dict[str, Any], archive["result"])["energy_mwh"]
    )


@pytest.mark.parametrize("delay_publication", [False, True])
def test_archive_excludes_acquisition_or_publication_exactly_at_daylight(
    monkeypatch: pytest.MonkeyPatch, delay_publication: bool
) -> None:
    system, plant = make_plant(monkeypatch)
    cutoff = forecast_cutoff(DAY, system.binding)
    previous = publish_snapshot(snapshot_for(system, DAY, collected_at=cutoff - timedelta(hours=2)))
    candidate = snapshot_for(
        system, DAY, collected_at=cutoff - timedelta(hours=1) if delay_publication else cutoff
    )
    publish_snapshot(candidate, published_at=cutoff)
    archive = archived_forecast(plant, DAY, 30, NOW)
    assert archive["snapshot_id"] == previous.pk
    assert not ForecastIssuance.objects.filter(snapshot=candidate, target_date=DAY).exists()


def test_archive_irradiance_uses_target_day_instead_of_collection_job_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    source = snapshot_for(system, DAY, power_kw=0.2)
    next_day = DAY + timedelta(days=1)
    next_source = snapshot_for(system, next_day, power_kw=0.4)
    for row in source.normalized_intervals + next_source.normalized_intervals:
        row["sunlight"] = "day"
    source.normalized_intervals.extend(next_source.normalized_intervals)
    source.job.target_date = next_day
    source.job.save(update_fields=["target_date"])
    source.save(update_fields=["normalized_intervals"])
    publish_snapshot(source)
    published = ForecastResult.objects.get(snapshot=source)
    assert published.payload["energy_kwh_m2"] == pytest.approx(9.6)
    assert published.daily_payloads[DAY.isoformat()]["energy_kwh_m2"] == pytest.approx(4.8)
    archive = archived_forecast(plant, DAY, 60, NOW)
    assert cast(dict[str, Any], archive["regional_irradiance"])["energy_kwh_m2"] == pytest.approx(
        4.8
    )
    assert not PlantForecast.objects.exists()


def test_archive_returns_outage_scenario_saved_at_collection_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    source = snapshot_for(system, DAY)
    monkeypatch.setattr(
        "pyracast.incidents.operations.timezone.now",
        lambda: source.collected_at - timedelta(minutes=30),
    )
    incident = save_incident(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        kind="panels",
        title="Known outage",
        notes="",
        starts_at=source.collected_at - timedelta(hours=1),
        ends_at=None,
        available_dc_mw=65,
        available_inverter_mw=None,
        available_export_mw=None,
    )
    publish_snapshot(source)
    monkeypatch.setattr("pyracast.incidents.operations.timezone.now", lambda: NOW)
    original = archived_forecast(plant, DAY, 60, NOW)
    scenario = cast(dict[str, Any], original["outage_scenario"])
    assert scenario["evaluated_at"] == source.collected_at.isoformat()
    assert scenario["incidents"][0]["id"] == incident.pk
    assert scenario["result"]["energy_mwh"] == pytest.approx(624)
    source.normalized_intervals[0]["power_kw"] = -1
    source.save(update_fields=["normalized_intervals"])
    replay = plant_outlook(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
        source="archived_forecast",
    )
    assert replay["outage_scenario"] == scenario
    assert replay["result"] == original["result"]
