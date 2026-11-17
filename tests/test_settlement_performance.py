from datetime import timedelta

import pytest
from django.test import Client

from pyracast.irradiance import local_day_bounds
from pyracast.metering.models import EpiasConnection, EpiasSnapshot
from pyracast.plants.models import CalibrationRun, ProductionObservation
from pyracast.plants.performance import forecast_performance
from pyracast.solar import daylight_window
from tests.support.domain import NOW
from tests.support.forecast_performance import DAY, publish, selected_factor
from tests.support.plant_operations import make_plant, snapshot_for
from tests.support.settlement_calibration import settlement

pytestmark = pytest.mark.django_db(transaction=True)


def test_performance_api_matches_settlement_with_explicit_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration = plant.current_configuration
    assert configuration is not None
    source = snapshot_for(system, DAY)
    publish(source)
    linked = EpiasConnection.objects.create(
        plant=plant,
        provider_plant_id=2,
        settlement_plant_id=77,
        eic="EIC",
        name="Plant",
        connected_by=system.member,
    )
    reported = settlement(linked, configuration, DAY, value=52)
    client = Client()
    client.force_login(system.member)
    url = (
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/performance/"
    )
    response = client.get(url, {"start_date": str(DAY), "end_date": str(DAY)})
    assert response.status_code == 200
    result = response.json()
    assert result["matched_days"] == 1
    assert result["metrics"]["daily_mae_mwh"] == pytest.approx(0)
    day = result["days"][0]
    assert day["measured_energy_mwh"] == 1248
    assert day["snapshot_id"] == source.pk
    assert day["manual_observation_ids"] == []
    assert day["settlement_snapshot_ids"] == [reported.pk]
    assert not ProductionObservation.objects.exists()


def test_manual_half_hour_excludes_whole_settlement_hour_and_provenance_tracks_daylight_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    config = plant.current_configuration
    assert config is not None
    publish(snapshot_for(system, DAY))
    linked = EpiasConnection.objects.create(
        plant=plant,
        provider_plant_id=2,
        settlement_plant_id=77,
        eic="EIC",
        name="Plant",
        connected_by=system.member,
    )
    reported = settlement(linked, config, DAY, value=52)
    windows = daylight_window(
        DAY,
        config.binding.timezone,
        config.binding.latitude,
        config.binding.longitude,
    ).windows
    start, _ = local_day_bounds(DAY, config.binding.timezone)
    hour = next(
        start + timedelta(hours=h)
        for h in range(24)
        if any(
            start + timedelta(hours=h) < end and start + timedelta(hours=h + 1) > begin
            for begin, end in windows
        )
    )
    first = ProductionObservation.objects.create(
        plant=plant,
        configuration=config,
        starts_at=hour,
        ends_at=hour + timedelta(minutes=30),
        energy_mwh=26,
        recorded_by=system.member,
    )
    missing = forecast_performance(plant, start_date=DAY, end_date=DAY, daylight_only=True)
    assert missing["matched_days"] == 0
    assert missing["days"][0]["reason"] == "incomplete_or_invalid_day"
    assert missing["days"][0]["manual_observation_ids"] == []
    assert missing["days"][0]["settlement_snapshot_ids"] == []
    second = ProductionObservation.objects.create(
        plant=plant,
        configuration=config,
        starts_at=hour + timedelta(minutes=30),
        ends_at=hour + timedelta(hours=1),
        energy_mwh=26,
        recorded_by=system.member,
    )
    ProductionObservation.objects.create(
        plant=plant,
        configuration=config,
        starts_at=start,
        ends_at=start + timedelta(hours=1),
        energy_mwh=999,
        quality="invalid",
        recorded_by=system.member,
    )
    complete = forecast_performance(plant, start_date=DAY, end_date=DAY, daylight_only=True)
    assert complete["matched_days"] == 1
    assert complete["metrics"]["daily_mae_mwh"] == pytest.approx(0)
    assert first.ends_at <= windows[0][0]
    assert complete["days"][0]["manual_observation_ids"] == [second.pk]
    assert complete["days"][0]["settlement_snapshot_ids"] == [reported.pk]


@pytest.mark.parametrize("invalid", ["identity", "basis"])
def test_settlement_performance_excludes_wrong_identity_and_non_export_basis(
    monkeypatch: pytest.MonkeyPatch,
    invalid: str,
) -> None:
    system, plant = make_plant(monkeypatch)
    config = plant.current_configuration
    assert config is not None
    publish(snapshot_for(system, DAY))
    linked = EpiasConnection.objects.create(
        plant=plant,
        provider_plant_id=2,
        settlement_plant_id=77,
        eic="EIC",
        name="Plant",
        connected_by=system.member,
    )
    settlement(linked, config, DAY)
    if invalid == "identity":
        linked.eic = "NEW-EIC"
        linked.save(update_fields=("eic",))
    else:
        config.measurement_basis = "ac_generation"
        config.save(update_fields=("measurement_basis",))
    result = forecast_performance(plant, start_date=DAY, end_date=DAY)
    assert result["matched_days"] == 0
    assert result["days"][0]["manual_observation_ids"] == []
    assert result["days"][0]["settlement_snapshot_ids"] == []


def test_settlement_revision_updates_assessment_while_preserving_forecast_and_calibration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    config = plant.current_configuration
    assert config is not None
    source = snapshot_for(system, DAY)
    original_intervals = source.normalized_intervals
    start, _ = local_day_bounds(DAY, config.binding.timezone)
    selected_factor(system, config, start - timedelta(days=1))
    publish(source)
    calibration = CalibrationRun.objects.get()
    linked = EpiasConnection.objects.create(
        plant=plant,
        provider_plant_id=2,
        settlement_plant_id=77,
        eic="EIC",
        name="Plant",
        connected_by=system.member,
    )
    old = settlement(linked, config, DAY, value=26)
    first = forecast_performance(plant, start_date=DAY, end_date=DAY)["days"][0]
    revised = settlement(linked, config, DAY, value=27, minute=1)
    second = forecast_performance(plant, start_date=DAY, end_date=DAY)["days"][0]
    assert first["settlement_snapshot_ids"] == [old.pk]
    assert second["settlement_snapshot_ids"] == [revised.pk]
    assert first["measured_energy_mwh"] == 624
    assert second["measured_energy_mwh"] == 648
    assert first["snapshot_id"] == second["snapshot_id"] == source.pk
    assert first["calibration_id"] == second["calibration_id"] == calibration.pk
    assert first["predicted_energy_mwh"] == second["predicted_energy_mwh"]
    source.refresh_from_db()
    calibration.refresh_from_db()
    assert source.normalized_intervals == original_intervals
    assert calibration.factor == 0.8
    assert calibration.completed_at is not None
    assert calibration.completed_at < NOW
    assert EpiasSnapshot.objects.filter(pk=old.pk, readings=old.readings).exists()
