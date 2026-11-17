from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from pyracast.accounts.models import Membership, Tenant
from pyracast.connections.models import SiteBinding
from pyracast.irradiance import local_day_bounds
from pyracast.plants.forecast_archive import prepare_forecast_issuances
from pyracast.plants.models import (
    CalibrationRun,
    ForecastIssuance,
    PlantConfiguration,
    PlantForecast,
    ProductionObservation,
)
from pyracast.plants.performance import forecast_performance
from pyracast.solar import daylight_window
from tests.support.domain import NOW
from tests.support.forecast_performance import DAY, measurements, publish, selected_factor
from tests.support.plant_operations import make_plant, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)


def test_performance_reads_frozen_native_clipping_and_keeps_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    assert plant.current_configuration_id is not None
    configuration = PlantConfiguration.objects.get(pk=plant.current_configuration_id)
    source = snapshot_for(system, DAY)
    for index, row in enumerate(source.normalized_intervals):
        row["power_kw"] = (80 if index % 2 == 0 else 120) / 130
    publish(source)
    measurements(system, configuration, 90)
    selected_factor(system, configuration, NOW)
    result = forecast_performance(plant, start_date=DAY, end_date=DAY)
    assert result["evaluation_kind"] == "archived_published_forecast"
    assert result["matched_days"] == result["requested_days"] == 1
    assert result["metrics"]["daily_mae_mwh"] == pytest.approx(0)
    assert result["days"][0]["predicted_energy_mwh"] == pytest.approx(2160)
    assert result["days"][0]["measured_energy_mwh"] == 2160
    assert result["days"][0]["factor"] == 1
    assert result["days"][0]["calibration_id"] is None
    assert result["days"][0]["snapshot_id"] == source.pk
    assert not PlantForecast.objects.exists()
    assert ForecastIssuance.objects.count() == 1


def test_daylight_performance_needs_no_night_readings_and_retains_boundary_energy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration = plant.current_configuration
    assert configuration is not None
    source = snapshot_for(system, DAY, power_kw=0.4)
    publish(source)
    measurements(system, configuration, 52)
    windows = daylight_window(
        DAY, system.binding.timezone, system.binding.latitude, system.binding.longitude
    ).windows
    for row in ProductionObservation.objects.filter(plant=plant):
        if not any(row.starts_at < end and row.ends_at > start for start, end in windows):
            row.delete()
    count = ProductionObservation.objects.filter(plant=plant).count()
    full = forecast_performance(plant, start_date=DAY, end_date=DAY)
    assert full["matched_days"] == 0
    result = forecast_performance(plant, start_date=DAY, end_date=DAY, daylight_only=True)
    assert result["metric_scope"] == "daylight_intervals"
    assert result["matched_days"] == 1
    assert result["days"][0]["measured_energy_mwh"] == count * 52
    assert result["days"][0]["predicted_energy_mwh"] == pytest.approx(count * 52)
    assert result["metrics"]["daily_mae_mwh"] == pytest.approx(0)
    client = Client()
    client.force_login(system.member)
    url = (
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/performance/"
    )
    response = client.get(
        url, {"start_date": DAY.isoformat(), "end_date": DAY.isoformat(), "daylight_only": "true"}
    )
    assert response.status_code == 200
    assert response.json()["matched_days"] == 1
    first = ProductionObservation.objects.filter(plant=plant).earliest("starts_at")
    first.delete()
    assert (
        forecast_performance(plant, start_date=DAY, end_date=DAY, daylight_only=True)[
            "matched_days"
        ]
        == 0
    )


def test_daylight_performance_without_configuration_is_explicitly_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plant = make_plant(monkeypatch)
    plant.current_configuration = None
    plant.save()
    result = forecast_performance(plant, start_date=DAY, end_date=DAY, daylight_only=True)
    assert result["matched_days"] == 0
    assert result["metrics"] is None
    assert result["metric_scope"] == "daylight_intervals"


def test_performance_uses_historical_activation_and_ignores_later_source_revisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    assert plant.current_configuration_id is not None
    configuration = PlantConfiguration.objects.get(pk=plant.current_configuration_id)
    configuration.export_limit_mw = None
    configuration.save()
    source = snapshot_for(system, DAY, power_kw=0.4)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    selected_factor(system, configuration, start - timedelta(days=1))
    publish(source)
    measurements(system, configuration, 41.6)
    later = snapshot_for(system, DAY, power_kw=0.9, collected_at=start + timedelta(hours=10))
    publish(later)
    actual = snapshot_for(system, DAY, power_kw=0.1, source_kind="estimated_actuals")
    publish(actual)
    result = forecast_performance(plant, start_date=DAY, end_date=DAY)
    assert result["metrics"]["daily_mae_mwh"] == pytest.approx(0, abs=1e-9)
    assert result["baseline_metrics"]["daily_mae_mwh"] == pytest.approx(249.6)
    assert result["days"][0]["factor"] == 0.8
    assert result["days"][0]["snapshot_id"] == source.pk


@pytest.mark.parametrize("kind", ["unpublished", "late_publication", "late_issue", "failed"])
def test_unavailable_sources_are_not_counted_as_issued_forecasts(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    system, plant = make_plant(monkeypatch)
    source = snapshot_for(system, DAY)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    windows = daylight_window(
        DAY, system.binding.timezone, system.binding.latitude, system.binding.longitude
    ).windows
    cutoff = windows[0][0] if windows else start
    if kind == "late_publication":
        publish(source, published_at=cutoff)
    elif kind == "late_issue":
        source.issued_at = cutoff
        publish(source)
    elif kind == "failed":
        source.job.status = "failed"
        source.job.save()
    result = forecast_performance(plant, start_date=DAY, end_date=DAY)
    assert result["matched_days"] == 0
    assert result["metrics"] is None
    assert result["days"][0]["reason"] == "forecast_not_available"


@pytest.mark.parametrize("kind", ["meter", "quality", "source", "invalid", "configuration"])
def test_performance_excludes_incomplete_days_and_retrospective_configuration(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    system, plant = make_plant(monkeypatch)
    assert plant.current_configuration_id is not None
    configuration = PlantConfiguration.objects.get(pk=plant.current_configuration_id)
    source = snapshot_for(system, DAY)
    measurements(system, configuration, 52)
    reason = "incomplete_or_invalid_day"
    if kind == "meter":
        ProductionObservation.objects.order_by("pk").first().delete()  # type: ignore[union-attr]
    elif kind == "quality":
        ProductionObservation.objects.update(quality="curtailed")
    elif kind == "source":
        source.normalized_intervals[0]["power_kw"] = None
        source.save()
    elif kind == "invalid":
        source.normalized_intervals[0]["power_kw"] = -1
        source.save()
        reason = "forecast_not_available"
    else:
        configuration.created_at = NOW
        configuration.save()
        reason = "forecast_not_available"
    if kind == "invalid":
        with pytest.raises(ValueError, match="Invalid source power"):
            publish(source)
    else:
        publish(source)
    result = forecast_performance(plant, start_date=DAY, end_date=DAY)
    assert result["matched_days"] == 0
    assert result["days"][0]["reason"] == reason


def test_performance_scope_and_date_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    client = Client()
    path = (
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/performance/"
    )
    assert client.get(path).status_code == 401
    other = User.objects.create_user("other")
    Membership.objects.create(user=other, tenant=Tenant.objects.create(name="Other"))
    client.force_login(other)
    assert client.get(path).status_code == 403
    client.force_login(system.member)
    response = client.get(path, {"start_date": DAY.isoformat(), "end_date": DAY.isoformat()})
    assert response.status_code == 200
    assert response.json()["requested_days"] == 1
    assert client.get(path, {"start_date": "bad"}).status_code == 400
    plant.deleted_at = NOW
    plant.save()
    assert client.get(path).status_code == 404
    plant.deleted_at = None
    plant.current_configuration = None
    plant.save()
    empty = forecast_performance(plant)
    assert empty["configuration_id"] is None
    assert empty["requested_days"] == 30
    assert empty["matched_days"] == 0
    for params in (
        {"start_date": DAY},
        {"start_date": DAY, "end_date": DAY - timedelta(days=1)},
        {"start_date": DAY, "end_date": DAY + timedelta(days=100)},
        {"start_date": NOW.date(), "end_date": NOW.date()},
        {"start_date": date(2020, 1, 1), "end_date": date(2020, 1, 2)},
    ):
        with pytest.raises(ValidationError):
            forecast_performance(
                plant, start_date=params.get("start_date"), end_date=params.get("end_date")
            )


def test_meter_basis_and_revisions_are_not_mixed(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    assert plant.current_configuration_id is not None
    old = PlantConfiguration.objects.get(pk=plant.current_configuration_id)
    measurements(system, old, 52)
    new = PlantConfiguration.objects.create(
        region_id=plant.region_id,
        plant=plant,
        binding=system.binding,
        revision=2,
        dc_capacity_mw=130,
        inverter_capacity_mw=100,
        measurement_basis="ac_generation",
        created_by=system.member,
    )
    PlantConfiguration.objects.filter(pk=new.pk).update(
        created_at=datetime(2026, 10, 1, tzinfo=UTC)
    )
    plant.current_configuration = new
    plant.save()
    publish(snapshot_for(system, DAY))
    result: dict[str, Any] = forecast_performance(plant, start_date=DAY, end_date=DAY)
    assert result["measurement_basis"] == "ac_generation"
    assert result["matched_days"] == 0


@pytest.mark.parametrize(("power", "energy", "bias"), [(0.4, 0, 1248), (0, 10, -240), (0, 0, 0)])
def test_valid_zero_days_remain_in_performance(
    monkeypatch: pytest.MonkeyPatch, power: float, energy: float, bias: float
) -> None:
    system, plant = make_plant(monkeypatch)
    assert plant.current_configuration_id is not None
    configuration = PlantConfiguration.objects.get(pk=plant.current_configuration_id)
    publish(snapshot_for(system, DAY, power_kw=power))
    measurements(system, configuration, energy)
    result = forecast_performance(plant, start_date=DAY, end_date=DAY)
    assert result["matched_days"] == 1
    assert result["metrics"]["daily_bias_mwh"] == pytest.approx(bias)
    if power == 0:
        assert result["metrics"]["source_positive_mae_mw"] is None


def test_frozen_power_does_not_reapply_calibration_or_current_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration = plant.current_configuration
    assert configuration is not None
    source = snapshot_for(system, DAY, power_kw=120 / 130)
    selected_factor(system, configuration, source.collected_at)
    selected = CalibrationRun.objects.get()
    publish(source)
    measurements(system, configuration, 96)
    selected_factor(system, configuration, source.collected_at + timedelta(hours=1))
    CalibrationRun.objects.exclude(pk=selected.pk).update(factor=0.5)
    source.normalized_intervals[0]["power_kw"] = 999
    source.save()

    def changed_model(*args: object, **kwargs: object) -> None:
        raise AssertionError("Archived performance must not calculate current model output.")

    monkeypatch.setattr("pyracast.plants.performance.power_intervals", changed_model)
    with CaptureQueriesContext(connection) as queries:
        result = forecast_performance(plant, start_date=DAY, end_date=DAY)
    unused_columns = (
        "raw_payload",
        "weather_payload",
        "normalized_intervals",
        "payloads",
        "outage_scenarios",
    )
    assert not any(
        f'"{column}"' in query["sql"]
        for query in queries.captured_queries
        for column in unused_columns
    )
    assert result["matched_days"] == 1
    assert result["days"][0]["predicted_energy_mwh"] == pytest.approx(2304)
    assert result["days"][0]["baseline_energy_mwh"] == pytest.approx(2400)
    assert result["days"][0]["calibration_id"] == selected.pk
    assert result["days"][0]["factor"] == 0.8
    assert result["metrics"]["daily_mae_mwh"] == pytest.approx(0)


def test_legacy_raw_snapshot_does_not_invent_a_published_electrical_issuance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration = plant.current_configuration
    assert configuration is not None
    source = snapshot_for(system, DAY)
    source.available_at = source.collected_at
    source.save()
    source.job.status = "succeeded"
    source.job.save()
    measurements(system, configuration, 52)
    result = forecast_performance(plant, start_date=DAY, end_date=DAY)
    assert result["days"][0]["reason"] == "forecast_not_available"
    assert result["missing_days"] == 1
    assert result["matched_days"] == 0
    assert result["metrics"] is None
    assert not ForecastIssuance.objects.exists()


def test_pre_sunrise_update_wins_over_later_published_older_acquisition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration = plant.current_configuration
    assert configuration is not None
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    old = snapshot_for(system, DAY, power_kw=0.2, collected_at=start - timedelta(hours=7))
    early = snapshot_for(system, DAY, power_kw=0.4, collected_at=start + timedelta(hours=3))
    publish(early)
    publish(old, published_at=start + timedelta(hours=4))
    publish(snapshot_for(system, DAY, power_kw=0.9, collected_at=start + timedelta(hours=10)))
    measurements(system, configuration, 52)
    result = forecast_performance(plant, start_date=DAY, end_date=DAY)
    assert result["days"][0]["snapshot_id"] == early.pk
    assert result["metrics"]["daily_mae_mwh"] == pytest.approx(0)


@pytest.mark.parametrize("change", ["capacity", "basis", "timezone"])
def test_archived_configuration_owns_measurements_without_mixing_meter_basis(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration = plant.current_configuration
    assert configuration is not None
    source = snapshot_for(system, DAY)
    publish(source)
    issuance = ForecastIssuance.objects.get()
    measurements(system, configuration, 52)
    binding = system.binding
    if change == "timezone":
        binding = SiteBinding.objects.get(pk=binding.pk)
        binding.pk = None
        binding.region_revision += 1
        binding.timezone = "Europe/Berlin"
        binding.save()
    later = PlantConfiguration.objects.create(
        region_id=plant.region_id,
        plant=plant,
        binding=binding,
        revision=2,
        dc_capacity_mw=200,
        inverter_capacity_mw=150,
        measurement_basis="ac_generation" if change == "basis" else "net_export",
        created_by=system.member,
    )
    plant.current_configuration = later
    plant.save()
    result = forecast_performance(plant, start_date=DAY, end_date=DAY)
    day = result["days"][0]
    assert day["configuration_id"] == configuration.pk
    if change == "capacity":
        assert day["issuance_id"] == issuance.pk
        assert result["matched_days"] == 1
        assert result["metrics"]["daily_mae_mwh"] == pytest.approx(0)
    else:
        assert day["reason"] == "configuration_not_comparable"
        assert result["missing_days"] == 1
        assert result["matched_days"] == 0
        assert result["metrics"] is None


def test_invalid_archived_interval_timestamps_are_excluded_without_raw_source_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration = plant.current_configuration
    assert configuration is not None
    source = snapshot_for(system, DAY)
    prepared = prepare_forecast_issuances(source)[0]
    issuance = ForecastIssuance.objects.create(
        plant=plant,
        configuration=configuration,
        snapshot=source,
        calibration_id=prepared.calibration_id,
        target_date=DAY,
        published_at=source.collected_at,
        cutoff_at=prepared.cutoff_at,
        model_version=prepared.model_version,
        factor=prepared.factor,
        payloads=prepared.payloads,
        outage_scenarios=prepared.outage_scenarios,
        native_intervals=[{**prepared.native_intervals[0], "start": "invalid"}],
    )
    measurements(system, configuration, 52)
    result = forecast_performance(plant, start_date=DAY, end_date=DAY)
    assert result["days"][0]["issuance_id"] == issuance.pk
    assert result["days"][0]["reason"] == "invalid_forecast_data"
    assert result["excluded_days"] == 1
    assert result["matched_days"] == result["missing_days"] == 0
    assert result["scored_intervals"] == result["scored_hours"] == 0
    assert result["metrics"] is result["baseline_metrics"] is None
    assert (
        ForecastIssuance.objects.get(pk=issuance.pk).native_intervals == issuance.native_intervals
    )


def test_archived_daylight_comparison_uses_the_original_configuration_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration = plant.current_configuration
    assert configuration is not None
    publish(snapshot_for(system, DAY))
    measurements(system, configuration, 52)
    original_windows = daylight_window(
        DAY, system.binding.timezone, system.binding.latitude, system.binding.longitude
    ).windows
    for row in ProductionObservation.objects.filter(plant=plant):
        if not any(row.starts_at < end and row.ends_at > start for start, end in original_windows):
            row.delete()
    hours = ProductionObservation.objects.filter(plant=plant).count()
    relocated = SiteBinding.objects.get(pk=system.binding.pk)
    relocated.pk = None
    relocated.region_revision += 1
    relocated.latitude = 40.7128
    relocated.longitude = -74.0060
    relocated.save()
    later = PlantConfiguration.objects.create(
        region_id=plant.region_id,
        plant=plant,
        binding=relocated,
        revision=2,
        dc_capacity_mw=130,
        export_limit_mw=100,
        measurement_basis=configuration.measurement_basis,
        created_by=system.member,
    )
    plant.current_configuration = later
    plant.save()
    result = forecast_performance(plant, start_date=DAY, end_date=DAY, daylight_only=True)
    assert result["days"][0]["configuration_id"] == configuration.pk
    assert result["matched_days"] == 1
    assert result["missing_days"] == result["excluded_days"] == 0
    assert result["days"][0]["predicted_energy_mwh"] == pytest.approx(hours * 52)
    assert result["days"][0]["measured_energy_mwh"] == hours * 52
    assert result["scored_hours"] == hours
    assert result["metrics"]["daily_mae_mwh"] == pytest.approx(0)
