from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.core.exceptions import ValidationError

from pyracast.connections.models import SiteBinding
from pyracast.irradiance import local_day_bounds
from pyracast.metering.models import EpiasConnection
from pyracast.plants.models import CalibrationRun, PlantConfiguration, ProductionObservation
from pyracast.plants.performance import forecast_performance
from tests.test_forecast_performance import DAY, measurements, publish, selected_factor
from tests.test_incident_calibration import incident
from tests.test_plant_operations import make_plant, snapshot_for
from tests.test_settlement_calibration import settlement

pytestmark = pytest.mark.django_db(transaction=True)


def test_default_day_ahead_evaluation_uses_latest_prior_day_forecast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration = plant.current_configuration
    assert configuration is not None
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    publish(snapshot_for(system, DAY, power_kw=0.2, collected_at=start - timedelta(hours=12)))
    latest = snapshot_for(system, DAY, power_kw=0.4, collected_at=start - timedelta(hours=1))
    publish(latest)
    measurements(system, configuration, 52)
    result = forecast_performance(plant, start_date=DAY, end_date=DAY)
    assert result["selection_policy"] == "latest-published-before-local-cutoff-v1"
    assert result["days"][0]["snapshot_id"] == latest.pk
    assert result["metrics"]["daily_mae_mwh"] == pytest.approx(0)


@pytest.mark.parametrize("late_field", ["collected_at", "available_at", "issued_at"])
def test_explicit_local_cutoff_selects_source_and_calibration_from_same_information_set(
    monkeypatch: pytest.MonkeyPatch, late_field: str
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration = plant.current_configuration
    assert configuration is not None
    cutoff = datetime.combine(DAY - timedelta(days=1), time(16), ZoneInfo(system.binding.timezone))
    early = snapshot_for(system, DAY, power_kw=0.4, collected_at=cutoff - timedelta(hours=1))
    publish(early)
    late = snapshot_for(system, DAY, power_kw=0.8, collected_at=cutoff)
    publish(late)
    setattr(late, late_field, cutoff + timedelta(seconds=1))
    late.save()
    selected_factor(system, configuration, cutoff)
    early_run = CalibrationRun.objects.latest("pk")
    selected_factor(system, configuration, cutoff + timedelta(seconds=1))
    CalibrationRun.objects.exclude(pk=early_run.pk).update(factor=0.6)
    measurements(system, configuration, 41.6)
    result = forecast_performance(plant, start_date=DAY, end_date=DAY, issuance_cutoff=time(16))
    day = result["days"][0]
    assert day["snapshot_id"] == early.pk
    assert day["selection_cutoff"] == cutoff.astimezone(UTC)
    assert day["factor"] == 0.8
    assert day["calibration_id"] == early_run.pk
    assert result["metrics"]["daily_mae_mwh"] == pytest.approx(0, abs=1e-9)
    assert result["baseline_metrics"]["daily_mae_mwh"] == pytest.approx(249.6)
    assert result["scored_intervals"] == 24
    assert result["scored_hours"] == 24
    assert result["matched_days"] == 1
    assert result["missing_days"] == result["excluded_days"] == 0


def test_cutoff_does_not_use_configuration_created_later_or_estimated_actuals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration = plant.current_configuration
    assert configuration is not None
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    late = snapshot_for(system, DAY, collected_at=start - timedelta(hours=1))
    publish(late)
    publish(snapshot_for(system, DAY, source_kind="estimated_actuals"))
    missing = forecast_performance(plant, start_date=DAY, end_date=DAY, issuance_cutoff=time(16))
    assert missing["days"][0]["reason"] == "forecast_not_available"
    assert missing["missing_days"] == 1
    assert missing["metrics"] is None
    PlantConfiguration.objects.filter(pk=configuration.pk).update(
        created_at=start - timedelta(hours=2)
    )
    unavailable = forecast_performance(
        plant, start_date=DAY, end_date=DAY, issuance_cutoff=time(16)
    )
    assert unavailable["days"][0]["reason"] == "configuration_not_available"
    assert unavailable["missing_days"] == 1


@pytest.mark.parametrize("provider", [False, True])
def test_reported_incident_does_not_hide_valid_operational_forecast_errors(
    monkeypatch: pytest.MonkeyPatch, provider: bool
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration = plant.current_configuration
    assert configuration is not None
    publish(snapshot_for(system, DAY, power_kw=0.4))
    if provider:
        connection = EpiasConnection.objects.create(
            plant=plant,
            provider_plant_id=2,
            settlement_plant_id=77,
            eic="EIC",
            name="Plant",
            connected_by=system.member,
        )
        settlement(connection, configuration, DAY, value=0)
    else:
        measurements(system, configuration, 0)
    start, end = local_day_bounds(DAY, system.binding.timezone)
    incident(system, plant, start, end)
    result = forecast_performance(plant, start_date=DAY, end_date=DAY)
    assert result["matched_days"] == 1
    assert result["metrics"]["daily_mae_mwh"] == pytest.approx(1248)
    assert result["days"][0]["measured_energy_mwh"] == 0


@pytest.mark.parametrize(
    ("comparison", "cutoff"),
    [("historical_model", time(16)), ("archived_forecast", time(16, tzinfo=UTC))],
)
def test_cutoff_rejects_retrospective_model_mode_and_offset_time(
    monkeypatch: pytest.MonkeyPatch, comparison: str, cutoff: time
) -> None:
    _, plant = make_plant(monkeypatch)
    with pytest.raises(ValidationError):
        forecast_performance(
            plant, start_date=DAY, end_date=DAY, comparison=comparison, issuance_cutoff=cutoff
        )


def test_cutoff_includes_exactly_available_snapshot_and_does_not_leak_later_calibration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration = plant.current_configuration
    assert configuration is not None
    cutoff = datetime.combine(DAY - timedelta(days=1), time(16), ZoneInfo(system.binding.timezone))
    source = snapshot_for(system, DAY, collected_at=cutoff, issued_at=cutoff)
    publish(source)
    selected_factor(system, configuration, cutoff + timedelta(microseconds=1))
    measurements(system, configuration, 52)
    result = forecast_performance(plant, start_date=DAY, end_date=DAY, issuance_cutoff=time(16))
    assert result["days"][0]["snapshot_id"] == source.pk
    assert result["days"][0]["factor"] == 1
    assert result["days"][0]["calibration_id"] is None
    assert result["metrics"] == result["baseline_metrics"]


def test_coverage_distinguishes_missing_input_from_excluded_observations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration = plant.current_configuration
    assert configuration is not None
    source = snapshot_for(system, DAY)
    publish(source)
    measurements(system, configuration, 52)
    ProductionObservation.objects.filter(plant=plant).update(quality="curtailed")
    result = forecast_performance(plant, start_date=DAY - timedelta(days=1), end_date=DAY)
    assert result["matched_days"] == 0
    assert result["missing_days"] == result["excluded_days"] == 1
    assert result["scored_intervals"] == result["scored_hours"] == 0
    assert result["metrics"] is result["baseline_metrics"] is None


@pytest.mark.parametrize("day", [date(2026, 3, 30), date(2026, 10, 26)])
def test_nonexistent_or_ambiguous_local_cutoff_is_rejected(
    monkeypatch: pytest.MonkeyPatch, day: date
) -> None:
    system, plant = make_plant(monkeypatch)
    SiteBinding.objects.filter(pk=system.binding.pk).update(timezone="Europe/Berlin")
    with pytest.raises(ValidationError, match="ambiguous or does not exist"):
        forecast_performance(plant, start_date=day, end_date=day, issuance_cutoff=time(2, 30))


def test_daylight_coverage_reports_only_the_whole_scored_boundary_intervals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration = plant.current_configuration
    assert configuration is not None
    publish(snapshot_for(system, DAY))
    measurements(system, configuration, 52)
    result = forecast_performance(plant, start_date=DAY, end_date=DAY, daylight_only=True)
    assert result["scored_hours"] == result["scored_intervals"]
    assert 0 < result["scored_hours"] < 24
    assert result["days"][0]["measured_energy_mwh"] == 52 * result["scored_hours"]
