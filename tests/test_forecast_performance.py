from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import Client

from pyracast.accounts.models import Membership, Tenant
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.irradiance import local_day_bounds
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    PlantConfiguration,
    PlantForecast,
    ProductionObservation,
)
from pyracast.plants.performance import forecast_performance
from pyracast.solar import daylight_window
from tests.test_domain import NOW, System
from tests.test_plant_operations import make_plant, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)
DAY = date(2026, 11, 8)


def publish(source: ForecastSnapshot) -> None:
    source.available_at = source.collected_at
    source.save()
    source.job.status = "succeeded"
    source.job.save()


def measurements(system: System, configuration: PlantConfiguration, energy: float) -> None:
    start, end = local_day_bounds(DAY, system.binding.timezone)
    while start < end:
        ProductionObservation.objects.create(
            plant_id=configuration.plant_id,
            configuration=configuration,
            starts_at=start,
            ends_at=start + timedelta(hours=1),
            energy_mwh=energy,
            recorded_by=system.member,
            source="meter",
        )
        start += timedelta(hours=1)


def selected_factor(system: System, configuration: PlantConfiguration, at: datetime) -> None:
    run = CalibrationRun.objects.create(
        plant_id=configuration.plant_id,
        configuration=configuration,
        binding=system.binding,
        requested_by=system.member,
        window_start=DAY - timedelta(days=10),
        window_end=DAY - timedelta(days=4),
        cutoff_at=at,
        completed_at=at,
        status="ready",
        factor=0.8,
    )
    CalibrationRun.objects.filter(pk=run.pk).update(created_at=at)
    activation = CalibrationActivation.objects.create(
        plant_id=configuration.plant_id,
        configuration=configuration,
        calibration=run,
        actor=system.member,
    )
    CalibrationActivation.objects.filter(pk=activation.pk).update(created_at=at)


def test_performance_replays_native_clipping_and_keeps_read_only(
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
    # This activation occurred after the forecast day and must not affect the replay.
    selected_factor(system, configuration, NOW)
    result = forecast_performance(plant, start_date=DAY, end_date=DAY)
    assert result["evaluation_kind"] == "historical_input_replay"
    assert result["matched_days"] == result["requested_days"] == 1
    assert result["metrics"]["daily_mae_mwh"] == pytest.approx(0)
    assert result["days"][0]["predicted_energy_mwh"] == pytest.approx(2160)
    assert result["days"][0]["measured_energy_mwh"] == 2160
    assert result["days"][0]["factor"] == 1
    assert result["days"][0]["calibration_id"] is None
    assert result["days"][0]["snapshot_id"] == source.pk
    assert not PlantForecast.objects.exists()


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
    publish(source)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    selected_factor(system, configuration, start - timedelta(days=1))
    measurements(system, configuration, 41.6)
    later = snapshot_for(system, DAY, power_kw=0.9, collected_at=start + timedelta(hours=1))
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
    publish(source)
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    if kind == "unpublished":
        source.available_at = None
    elif kind == "late_publication":
        source.available_at = start
    elif kind == "late_issue":
        source.issued_at = start
    else:
        source.job.status = "failed"
        source.job.save()
    source.save()
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
    publish(source)
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
        reason = "invalid_forecast_data"
    else:
        configuration.created_at = NOW
        configuration.save()
        reason = "configuration_not_available"
    result = forecast_performance(plant, start_date=DAY, end_date=DAY)
    assert result["matched_days"] == 0
    assert result["days"][0]["reason"] == reason


def test_performance_scope_and_date_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    client = Client()
    path = (
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/performance/"
    )
    assert client.get(path).status_code == 403
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
