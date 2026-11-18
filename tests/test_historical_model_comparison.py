from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import Client

from pyracast.accounts.models import Membership, Tenant
from pyracast.irradiance import local_day_bounds
from pyracast.metering.models import EpiasConnection
from pyracast.plants.models import CalibrationRun, ProductionObservation
from pyracast.plants.performance import forecast_performance
from pyracast.solar import Daylight
from tests.support.domain import NOW
from tests.support.forecast_performance import selected_factor
from tests.support.plant_operations import make_plant
from tests.support.plant_training import actual_snapshot_for
from tests.support.provisional_measurements import DAY, acquisition
from tests.support.settlement_calibration import settlement

pytestmark = pytest.mark.django_db(transaction=True)


def test_historical_daylight_comparison_retains_whole_boundary_hours_and_native_clipping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    config = plant.current_configuration
    assert config is not None
    config.created_at = NOW
    config.save(update_fields=("created_at",))
    start, _ = local_day_bounds(DAY, system.binding.timezone)
    sunrise = start + timedelta(hours=7, minutes=15)
    sunset = start + timedelta(hours=8, minutes=45)
    monkeypatch.setattr(
        "pyracast.plants.performance.daylight_window",
        lambda *_args: Daylight(((sunrise, sunset),), sunrise, sunset, "normal"),
    )
    reference = actual_snapshot_for(system, DAY)
    native_mw = {14: 80, 15: 120, 16: 140, 17: 40}
    for index, interval in enumerate(reference.normalized_intervals):
        interval["power_kw"] = native_mw[index] / 130 if index in native_mw else None
    reference.save(update_fields=("normalized_intervals",))
    observations = [
        ProductionObservation.objects.create(
            plant=plant,
            configuration=config,
            starts_at=start + timedelta(hours=hour),
            ends_at=start + timedelta(hours=hour + 1),
            energy_mwh=energy,
            recorded_by=system.member,
        )
        for hour, energy in ((7, 91), (8, 72))
    ]
    result = forecast_performance(
        plant, start_date=DAY, end_date=DAY, comparison="historical_model", daylight_only=True
    )
    assert result["metric_scope"] == "daylight_intervals"
    assert result["matched_days"] == 1
    day = result["days"][0]
    assert day["snapshot_id"] == reference.pk
    assert day["source_kind"] == "estimated_actuals"
    assert day["manual_observation_ids"] == [row.pk for row in observations]
    assert day["measured_energy_mwh"] == 91 + 72
    assert day["predicted_energy_mwh"] == pytest.approx((80 + 100 + 100 + 40) / 2)
    assert day["baseline_energy_mwh"] == pytest.approx(160)
    assert day["error_mwh"] == pytest.approx(-3)
    assert (
        forecast_performance(plant, start_date=DAY, end_date=DAY, comparison="historical_model")[
            "matched_days"
        ]
        == 0
    )


def test_historical_model_applies_the_calibration_fitted_for_that_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    config = plant.current_configuration
    assert config is not None
    config.created_at = NOW
    config.save(update_fields=("created_at",))
    reference = actual_snapshot_for(system, DAY, power_kw=0.2)
    connection = EpiasConnection.objects.create(
        plant=plant,
        provider_plant_id=2,
        settlement_plant_id=77,
        eic="EIC",
        name="Plant",
        connected_by=system.member,
    )
    production = acquisition((config, connection))
    selected_factor(system, config, NOW - timedelta(days=20))
    CalibrationRun.objects.update(result={"training_day_dates": [DAY.isoformat()]})
    client = Client()
    client.force_login(system.member)
    url = (
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/performance/"
    )
    query = {"start_date": str(DAY), "end_date": str(DAY), "comparison": "historical_model"}
    response = client.get(url, query)
    assert response.status_code == 200
    result = response.json()
    assert result["evaluation_kind"] == "retrospective_model_comparison"
    assert result["matched_days"] == 1
    day = result["days"][0]
    assert day["factor"] == 0.8
    assert day["calibration_id"] == CalibrationRun.objects.get().pk
    assert day["source_kind"] == "estimated_actuals"
    assert day["historical_configuration_assumed"] is True
    assert day["provisional_production"] is True
    assert day["provisional_snapshot_ids"] == [production.pk]
    assert day["settlement_snapshot_ids"] == []
    assert day["snapshot_id"] == reference.pk
    assert day["measured_energy_mwh"] == 624
    assert day["predicted_energy_mwh"] == pytest.approx(0.8 * 624)
    assert day["baseline_energy_mwh"] == pytest.approx(624)
    archived = client.get(url, {**query, "comparison": "archived_forecast"}).json()
    assert archived["matched_days"] == 0
    assert archived["days"][0]["reason"] == "forecast_not_available"


def test_historical_comparison_preserves_settlement_zero_and_latest_missing_hours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    config = plant.current_configuration
    assert config is not None
    actual_snapshot_for(system, DAY, power_kw=0)
    connection = EpiasConnection.objects.create(
        plant=plant,
        provider_plant_id=2,
        settlement_plant_id=77,
        eic="EIC",
        name="Plant",
        connected_by=system.member,
    )
    source = (config, connection)
    acquisition(source, values=["9"] * 24)
    settled = acquisition(source, "settlement_injection", values=["0"] * 24)
    result = forecast_performance(
        plant, start_date=DAY, end_date=DAY, comparison="historical_model"
    )
    day = result["days"][0]
    assert day["status"] == "matched"
    assert day["measured_energy_mwh"] == day["predicted_energy_mwh"] == 0
    assert day["provisional_production"] is False
    assert day["settlement_snapshot_ids"] == [settled.pk]
    acquisition(source, "settlement_injection", minute=1, values=[None] * 24)
    acquisition(source, minute=1, values=[None] * 24)
    missing = forecast_performance(
        plant, start_date=DAY, end_date=DAY, comparison="historical_model"
    )
    assert missing["matched_days"] == 0
    assert missing["days"][0]["measured_energy_mwh"] is None


@pytest.mark.parametrize("field", ["collected_at", "available_at", "issued_at"])
def test_historical_comparison_excludes_future_reference_publication(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    system, plant = make_plant(monkeypatch)
    source = actual_snapshot_for(system, DAY)
    setattr(source, field, NOW + timedelta(seconds=1))
    source.save(update_fields=(field,))
    result = forecast_performance(
        plant, start_date=DAY, end_date=DAY, comparison="historical_model"
    )
    assert result["matched_days"] == 0
    assert result["days"][0]["reason"] == "estimated_actuals_not_available"
    assert result["days"][0]["snapshot_id"] is None


def test_historical_comparison_uses_native_hours_across_local_date_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    config = plant.current_configuration
    assert config is not None
    connection = EpiasConnection.objects.create(
        plant=plant,
        provider_plant_id=2,
        settlement_plant_id=77,
        eic="EIC",
        name="Plant",
        connected_by=system.member,
    )
    first = settlement(connection, config, DAY)
    second = settlement(connection, config, DAY + timedelta(days=1))
    system.binding.timezone = "Europe/London"
    system.binding.save(update_fields=("timezone",))
    actual_snapshot_for(system, DAY, power_kw=0.2)
    result = forecast_performance(
        plant, start_date=DAY, end_date=DAY, comparison="historical_model"
    )
    day = result["days"][0]
    assert result["timezone"] == "Europe/London"
    assert result["matched_days"] == 1
    assert day["measured_energy_mwh"] == 624
    assert day["predicted_energy_mwh"] == pytest.approx(624)
    assert day["settlement_snapshot_ids"] == [first.pk, second.pk]
    second.delete()
    missing = forecast_performance(
        plant, start_date=DAY, end_date=DAY, comparison="historical_model"
    )
    assert missing["matched_days"] == 0
    assert missing["days"][0]["measured_energy_mwh"] is None


def test_historical_comparison_keeps_tenant_access_and_rejects_unknown_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    client = Client()
    url = (
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/performance/"
    )
    assert client.get(url, {"comparison": "historical_model"}).status_code == 401
    outsider = User.objects.create_user("outsider")
    Membership.objects.create(user=outsider, tenant=Tenant.objects.create(name="Other"))
    client.force_login(outsider)
    assert client.get(url, {"comparison": "historical_model"}).status_code == 403
    client.force_login(system.member)
    assert client.get(url, {"comparison": "unknown"}).status_code == 400
    with pytest.raises(ValidationError, match="Unknown comparison mode"):
        forecast_performance(plant, comparison="unknown")
