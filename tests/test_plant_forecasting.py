from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client

from pyracast.plants import forecasting
from pyracast.plants.activation import activate_calibration
from pyracast.plants.forecasting import plant_forecast, power_intervals, power_limit
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    ForecastIssuance,
    PlantConfiguration,
)
from pyracast.plants.numerics import MODEL_VERSION
from pyracast.plants.operations import revise_plant
from tests.support.domain import NOW
from tests.support.invariants import stored_again
from tests.support.plant_operations import make_plant, publish_snapshot, ready_run, snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)
DAY = date(2026, 11, 9)


def test_native_clipping_zero_missingness_and_immutable_issuance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    source = snapshot_for(system, DAY, power_kw=0)
    source.normalized_intervals[0]["power_kw"] = 120 / 130
    source.normalized_intervals[1]["power_kw"] = 80 / 130
    stored_again(source)
    publish_snapshot(source)
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
    assert cast(dict[str, Any], result["regional_irradiance"])["complete"] is False
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
    assert ForecastIssuance.objects.count() == 1
    source.normalized_intervals[1]["power_kw"] = None
    stored_again(source)
    partial = plant_forecast(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=30,
    )
    missing = cast(dict[str, Any], partial["result"])
    assert missing["energy_mwh"] == pytest.approx(90)
    assert missing["intervals"][1]["energy_mwh"] == pytest.approx(40)


def test_snapshot_selection_excludes_future_issue_collection_and_actuals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    first = publish_snapshot(snapshot_for(system, DAY))
    latest = publish_snapshot(
        snapshot_for(system, DAY, power_kw=0.3, collected_at=first.collected_at)
    )
    publish_snapshot(
        snapshot_for(
            system,
            DAY,
            power_kw=0.9,
            collected_at=first.collected_at + timedelta(minutes=1),
            issued_at=NOW,
        )
    )
    publish_snapshot(snapshot_for(system, DAY, power_kw=0.9, collected_at=NOW))
    publish_snapshot(
        snapshot_for(
            system,
            DAY,
            power_kw=0.9,
            collected_at=first.collected_at + timedelta(minutes=2),
            source_kind="estimated_actuals",
        )
    )
    result = plant_forecast(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
    )
    assert cast(dict[str, Any], result["result"])["provenance"]["snapshot_id"] == latest.pk
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
    publish_snapshot(source)
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


def test_historical_issuance_survives_revision_at_both_resolutions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    publish_snapshot(snapshot_for(system, DAY))
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
    expected = cast(dict[str, Any], original["result"])
    expected["quality_flags"].append("previous_configuration")
    assert historical["result"] == expected
    assert historical["freshness"] == "archived"
    half_hourly = plant_forecast(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=30,
    )
    assert half_hourly["configuration_id"] == original["configuration_id"]
    assert cast(dict[str, Any], half_hourly["result"])["energy_mwh"] == expected["energy_mwh"]


def test_archive_reader_never_recalculates_a_published_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    source = publish_snapshot(snapshot_for(system, DAY))
    frozen = ForecastIssuance.objects.get(snapshot=source).payloads["60"]

    def unavailable(*args: Any, **kwargs: Any) -> dict[str, object]:
        raise AssertionError("An archive must not execute the current transfer model.")

    monkeypatch.setattr(forecasting, "forecast", unavailable)
    result = plant_forecast(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
    )
    assert cast(dict[str, Any], result["result"])["energy_mwh"] == frozen["energy_mwh"]


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
    publish_snapshot(snapshot_for(system, DAY))
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
        power_intervals(source, configuration, model_version=MODEL_VERSION)


def test_corrupt_source_metadata_and_changed_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    assert plant.current_configuration_id is not None
    source = snapshot_for(system, DAY)
    configuration = PlantConfiguration.objects.select_related("binding").get(
        pk=plant.current_configuration_id
    )
    configuration.binding.capacity_kw = float("inf")
    with pytest.raises(ValueError, match="Invalid reference capacity"):
        power_intervals(source, configuration, model_version=MODEL_VERSION)
    configuration.binding.capacity_kw = 1
    source.normalized_intervals[0]["start"] = 1
    with pytest.raises(ValueError, match="Invalid source interval timestamps"):
        power_intervals(source, configuration, model_version=MODEL_VERSION)
    system.region.timezone = "UTC"
    system.region.save()
    replay = plant_forecast(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
    )
    assert replay["result"] is None
    assert replay["unavailable_reason"] == "forecast_unavailable"


def test_latest_published_pre_daylight_source_replaces_previous_forecast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    acquired = datetime(2026, 11, 8, 13, 1, tzinfo=UTC)
    old = publish_snapshot(snapshot_for(system, DAY, collected_at=acquired - timedelta(days=1)))
    first = publish_snapshot(snapshot_for(system, DAY, collected_at=acquired))
    first.job.scheduled_for = acquired - timedelta(minutes=1)
    first.job.save()
    newest = publish_snapshot(
        snapshot_for(system, DAY, power_kw=0.9, collected_at=datetime(2026, 11, 8, 20, tzinfo=UTC))
    )
    unavailable = plant_forecast(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=DAY,
        resolution_minutes=60,
        as_of=acquired - timedelta(seconds=1),
    )
    assert unavailable["snapshot_id"] == old.pk
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
    assert available["snapshot_id"] == first.pk
    assert available["result"] != later["result"]
    assert provenance["snapshot_id"] == newest.pk
    assert provenance["snapshot_id"] != old.pk
    assert provenance["selection_policy"] == "latest-published-pre-daylight-issuance-v1"
    assert provenance["late"] is False
    assert provenance["lead_hours"] == pytest.approx(1)


@pytest.mark.parametrize("power_kw", [0.4, 2.0])
@pytest.mark.parametrize("explicit_as_of", [False, True])
def test_finite_capacity_overflow_returns_validation_without_saving_forecast(
    monkeypatch: pytest.MonkeyPatch,
    power_kw: float,
    explicit_as_of: bool,
) -> None:
    system, plant = make_plant(monkeypatch)
    target = DAY + timedelta(days=1)
    source = publish_snapshot(snapshot_for(system, target, power_kw=power_kw, collected_at=NOW))
    frozen = ForecastIssuance.objects.get(snapshot=source).payloads["60"]
    plant = revise_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        name="Large finite capacity",
        dc_capacity_mw=1e308,
    )
    client = Client()
    client.force_login(system.member)
    query = {"date": target.isoformat(), "resolution_minutes": "60"}
    if explicit_as_of:
        query["as_of"] = NOW.isoformat()
    response = client.get(
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/forecast/",
        query,
    )
    if explicit_as_of:
        assert response.status_code == 200
        assert response.json()["result"]["energy_mwh"] == frozen["energy_mwh"]
        assert response.json()["configuration_id"] != plant.current_configuration_id
        return
    assert response.status_code == 400
    assert response.json() == {
        "code": "calculation_out_of_range",
        "message": (
            "This forecast could not be calculated. "
            "Choose another day or contact your administrator."
        ),
    }


def test_pure_projection_claims_archive_selection_only_after_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration = plant.current_configuration
    assert configuration is not None
    source = snapshot_for(system, DAY)
    projected = forecasting.forecast_payload(
        configuration, source, None, DAY, 60, model_version=MODEL_VERSION
    )
    provenance = cast(dict[str, Any], projected["provenance"])
    assert provenance["selection_policy"] == "single-snapshot-forecast-projection-v1"
    assert "published_at" not in provenance
    assert not ForecastIssuance.objects.exists()
    publish_snapshot(source)
    issued = ForecastIssuance.objects.get(snapshot=source)
    published = issued.payloads["60"]
    assert (
        published["provenance"]["selection_policy"] == "latest-published-pre-daylight-issuance-v1"
    )
    assert published["provenance"]["published_at"] == source.collected_at.isoformat()
    assert published["energy_mwh"] == projected["energy_mwh"]
