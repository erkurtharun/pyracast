from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client

from pyracast.forecasts.models import ForecastResult
from pyracast.plants import forecasting
from pyracast.plants.forecasting import plant_forecast, power_intervals, power_limit
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    PlantConfiguration,
    PlantForecast,
)
from pyracast.plants.numerics import forecast
from pyracast.plants.operations import activate_calibration, create_plant, revise_plant
from tests.test_domain import NOW
from tests.test_plant_operations import make_plant, ready_run, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)
DAY = date(2026, 11, 9)


def test_native_clipping_zero_missingness_and_immutable_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    source = snapshot_for(system, DAY, power_kw=0)
    source.normalized_intervals[0]["power_kw"] = 120 / 130
    source.normalized_intervals[1]["power_kw"] = 80 / 130
    source.save()
    ForecastResult.objects.create(
        snapshot=source, payload={"energy_kwh_m2": None, "complete": False}, published_at=NOW
    )
    result = plant_forecast(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
    )
    numerical = cast(dict[str, Any], result["result"])
    assert numerical["energy_mwh"] == pytest.approx(90)
    assert numerical["intervals"][0]["energy_mwh"] == pytest.approx(90)
    assert numerical["intervals"][1]["energy_mwh"] == 0
    assert numerical["complete"]
    assert result["regional_irradiance"] == {
        "energy_kwh_m2": None,
        "partial_energy_kwh_m2": None,
        "complete": False,
        "quality_flags": None,
    }
    assert "geometry_not_modeled" in numerical["quality_flags"]
    as_of = source.collected_at + timedelta(minutes=10)
    second = plant_forecast(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
        as_of=as_of,
    )
    assert second["result"] == numerical
    assert second["as_of"] != result["as_of"]
    assert PlantForecast.objects.count() == 1
    source.normalized_intervals[1]["power_kw"] = None
    source.save()
    partial = plant_forecast(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=30,
    )
    missing = cast(dict[str, Any], partial["result"])
    assert missing["energy_mwh"] is None
    assert missing["partial_energy_mwh"] == pytest.approx(50)
    assert missing["intervals"][1]["energy_mwh"] is None


def test_snapshot_selection_excludes_future_issue_collection_and_actuals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    first = snapshot_for(system, DAY)
    snapshot_for(system, DAY, power_kw=0.3, collected_at=first.collected_at)
    snapshot_for(
        system,
        DAY,
        power_kw=0.9,
        collected_at=first.collected_at + timedelta(minutes=1),
        issued_at=NOW,
    )
    snapshot_for(system, DAY, power_kw=0.9, collected_at=NOW)
    snapshot_for(
        system,
        DAY,
        power_kw=0.9,
        collected_at=first.collected_at + timedelta(minutes=2),
        source_kind="estimated_actuals",
    )
    result = plant_forecast(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
    )
    assert cast(dict[str, Any], result["result"])["provenance"]["snapshot_id"] == first.pk
    assert result["unavailable_reason"] is None


def test_absent_configuration_and_source_are_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    result = plant_forecast(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=30,
    )
    assert result["freshness"] == "missing"
    assert result["unavailable_reason"] == "forecast_unavailable"
    assert result["result"] is None
    PlantConfiguration.objects.update(created_at=NOW)
    result = plant_forecast(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
    )
    assert result["freshness"] == "missing"
    assert result["unavailable_reason"] == "configuration_unavailable"
    assert result["configuration_id"] is None
    assert result["calibration_id"] is None
    assert result["result"] is None


@pytest.mark.parametrize(
    "arguments",
    [
        {"resolution_minutes": 15},
        {"resolution_minutes": True},
        {"target_date": date(2025, 1, 1)},
        {"target_date": date(2027, 1, 1)},
        {"as_of": NOW},
        {"as_of": NOW + timedelta(days=1)},
        {"as_of": datetime(2026, 11, 8)},
    ],
)
def test_forecast_input_bounds(monkeypatch: pytest.MonkeyPatch, arguments: dict[str, Any]) -> None:
    system, plant = make_plant(monkeypatch)
    values: dict[str, Any] = {"target_date": DAY, "resolution_minutes": 60}
    values.update(arguments)
    with pytest.raises(ValidationError):
        plant_forecast(system.member, system.tenant.pk, system.region.pk, plant.pk, **values)
    stranger = User.objects.create(username="stranger")
    with pytest.raises(PermissionDenied):
        plant_forecast(stranger, system.tenant.pk, system.region.pk, plant.pk, **values)


@pytest.mark.parametrize("future_field", [None, "completed_at", "cutoff_at", "created_at"])
def test_active_calibration_is_available_at_issuance(
    monkeypatch: pytest.MonkeyPatch, future_field: str | None
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
    if future_field is not None:
        CalibrationRun.objects.filter(pk=run.pk).update(**{future_field: NOW})
    result = plant_forecast(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
    )
    assert result["calibration_id"] == (run.pk if future_field is None else None)
    assert cast(dict[str, Any], result["result"])["factor"] == (0.5 if future_field is None else 1)


def test_historical_cached_result_survives_revision_but_new_output_cannot_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    snapshot_for(system, DAY)
    original = plant_forecast(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
    )
    revise_plant(
        system.member, system.tenant.pk, system.region.pk, plant.pk, name="New", dc_capacity_mw=140
    )
    historical = plant_forecast(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
    )
    assert historical["result"] == original["result"]
    assert historical["freshness"] == "stale"
    with pytest.raises(ValidationError, match="configuration_changed"):
        plant_forecast(
            system.member,
            system.tenant.pk,
            system.region.pk,
            plant.pk,
            target_date=DAY,
            resolution_minutes=30,
        )


def test_configuration_change_during_calculation_cannot_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    snapshot_for(system, DAY)
    original = forecast

    def change(*args: Any, **kwargs: Any) -> dict[str, object]:
        revise_plant(
            system.member,
            system.tenant.pk,
            system.region.pk,
            plant.pk,
            name="New",
            dc_capacity_mw=140,
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(forecasting, "forecast", change)
    with pytest.raises(ValidationError, match="configuration_changed"):
        plant_forecast(
            system.member,
            system.tenant.pk,
            system.region.pk,
            plant.pk,
            target_date=DAY,
            resolution_minutes=60,
        )
    assert not PlantForecast.objects.exists()


def test_measurement_basis_determines_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    assert plant.current_configuration_id is not None
    configuration = PlantConfiguration.objects.get(pk=plant.current_configuration_id)
    configuration.inverter_capacity_mw = 110
    assert power_limit(configuration) == 100
    configuration.measurement_basis = "ac_generation"
    assert power_limit(configuration) == 110
    configuration.inverter_capacity_mw = None
    assert power_limit(configuration) is None
    configuration.save()
    snapshot_for(system, DAY)
    result = plant_forecast(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
    )
    assert "output_limit_unknown" in cast(dict[str, Any], result["result"])["quality_flags"]


@pytest.mark.parametrize("value", [True, -1, float("inf"), "bad"])
def test_corrupt_source_power_is_rejected(monkeypatch: pytest.MonkeyPatch, value: object) -> None:
    system, plant = make_plant(monkeypatch)
    assert plant.current_configuration_id is not None
    source = snapshot_for(system, DAY)
    source.normalized_intervals[0]["power_kw"] = value
    configuration = PlantConfiguration.objects.select_related("binding").get(
        pk=plant.current_configuration_id
    )
    with pytest.raises(ValueError, match="Invalid source power"):
        power_intervals(source, configuration)


def test_corrupt_source_metadata_and_changed_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    assert plant.current_configuration_id is not None
    source = snapshot_for(system, DAY)
    configuration = PlantConfiguration.objects.select_related("binding").get(
        pk=plant.current_configuration_id
    )
    configuration.binding.capacity_kw = float("inf")
    with pytest.raises(ValueError, match="Invalid reference capacity"):
        power_intervals(source, configuration)
    configuration.binding.capacity_kw = 1
    source.normalized_intervals[0]["start"] = 1
    with pytest.raises(ValueError, match="Invalid source interval timestamps"):
        power_intervals(source, configuration)
    system.region.timezone = "UTC"
    system.region.save()
    with pytest.raises(ValidationError, match="configured local day start"):
        plant_forecast(
            system.member,
            system.tenant.pk,
            system.region.pk,
            plant.pk,
            target_date=DAY,
            resolution_minutes=60,
        )


def test_first_available_day_ahead_source_remains_stable_after_late_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    acquired = datetime(2026, 11, 8, 13, 1, tzinfo=UTC)
    old = snapshot_for(system, DAY, collected_at=acquired - timedelta(days=1))
    first = snapshot_for(system, DAY, collected_at=acquired)
    first.job.scheduled_slot = acquired - timedelta(minutes=1)
    first.job.save()
    snapshot_for(system, DAY, power_kw=0.9, collected_at=datetime(2026, 11, 8, 20, tzinfo=UTC))
    unavailable = plant_forecast(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
        as_of=acquired - timedelta(seconds=1),
    )
    assert unavailable["result"] is None
    available = plant_forecast(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
        as_of=acquired,
    )
    later = plant_forecast(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
    )
    provenance = cast(dict[str, Any], later["result"])["provenance"]
    assert available["result"] == later["result"]
    assert provenance["snapshot_id"] == first.pk
    assert provenance["snapshot_id"] != old.pk
    assert provenance["selection_policy"] == "first-available-day-ahead-v1"
    assert provenance["late"] is True
    assert provenance["lead_hours"] == pytest.approx(8 - 1 / 60)


@pytest.mark.parametrize("power_kw", [0.4, 2.0])
def test_finite_capacity_overflow_returns_validation_without_saving_forecast(
    monkeypatch: pytest.MonkeyPatch,
    power_kw: float,
) -> None:
    system, _ = make_plant(monkeypatch)
    plant = create_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        name="Large finite capacity",
        dc_capacity_mw=1e308,
    )
    target = DAY + timedelta(days=1)
    snapshot_for(system, target, power_kw=power_kw, collected_at=NOW)
    client = Client()
    client.force_login(system.member)
    response = client.get(
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/forecast/",
        {"date": target.isoformat(), "resolution_minutes": "60"},
    )
    assert response.status_code == 400
    assert response.json() == {"code": "invalid_request", "detail": ["calculation_out_of_range"]}
    assert not PlantForecast.objects.exists()
