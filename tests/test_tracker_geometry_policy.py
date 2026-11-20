from datetime import date, datetime, timedelta
from typing import Any, cast

import pytest

from pyracast.connections.models import SiteBinding
from pyracast.metering.models import EpiasConnection
from pyracast.plants import tracking
from pyracast.plants.activation import activate_calibration
from pyracast.plants.calibration_data import build_calibration_dataset
from pyracast.plants.forecasting import forecast_payload, power_limit
from pyracast.plants.history import plant_outlook
from pyracast.plants.incident_forecasts import incident_scenario
from pyracast.plants.models import CalibrationRun, ForecastIssuance, Plant, PlantConfiguration
from pyracast.plants.numerics import DERATING_POLICY, MODEL_VERSION
from pyracast.plants.performance import forecast_performance
from pyracast.plants.tracking import MODEL_V1, MODEL_V2
from pyracast.plants.training import run_calibration
from pyracast.plants.transfer import reference_power_mw
from tests.support.domain import NOW, System
from tests.support.forecast_performance import selected_factor
from tests.support.incident_calibration import incident
from tests.support.plant_operations import (
    make_plant,
    publish_snapshot,
    ready_run,
    request_run,
    snapshot_for,
)
from tests.support.plant_training import actual_snapshot_for, history
from tests.support.provisional_measurements import DAY as MEASURED_DAY
from tests.support.provisional_measurements import acquisition

pytestmark = pytest.mark.django_db(transaction=True)

DAY = date(2026, 11, 10)
VERSIONS = [MODEL_V1, MODEL_V2]


def _tracking_plant(monkeypatch: pytest.MonkeyPatch) -> tuple[System, Plant, PlantConfiguration]:
    system, plant = make_plant(monkeypatch)
    SiteBinding.objects.filter(pk=system.binding.pk).update(dc_capacity_kw=1)
    PlantConfiguration.objects.filter(plant=plant).update(mounting_type="tracking")
    return system, plant, _configuration(plant)


def _configuration(plant: Plant) -> PlantConfiguration:
    return PlantConfiguration.objects.select_related("binding").get(plant=plant)


def _default(monkeypatch: pytest.MonkeyPatch, model: str) -> None:
    monkeypatch.setattr(tracking, "DEFAULT_MODEL", model)


def _calibration(system: System, plant: Plant, model: str) -> CalibrationRun:
    run = ready_run(system, plant)
    run.result["transfer_model"] = model
    run.save(update_fields=("result",))
    return run


def _activate(system: System, plant: Plant, run: CalibrationRun) -> None:
    activate_calibration(system.member, system.tenant.pk, system.region.pk, plant.pk, run.pk)


def _outlook(system: System, plant: Plant, target_date: date, **options: Any) -> dict[str, Any]:
    return plant_outlook(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        target_date=target_date,
        resolution_minutes=60,
        **options,
    )


def _powers(payload: dict[str, object]) -> list[object]:
    return [row["mean_power_mw"] for row in cast(list[dict[str, object]], payload["intervals"])]


@pytest.mark.parametrize("default", VERSIONS)
def test_new_forecasts_and_calibrations_record_and_use_the_default_version(
    monkeypatch: pytest.MonkeyPatch, default: str
) -> None:
    system, plant, configuration = _tracking_plant(monkeypatch)
    _default(monkeypatch, default)
    snapshot = publish_snapshot(snapshot_for(system, DAY, collected_at=NOW))
    issuance = ForecastIssuance.objects.get()
    payload = issuance.payloads["30"]
    assert issuance.model_version == payload["model_version"] == default
    limit = power_limit(configuration)
    assert limit is not None
    assert _powers(payload) == [
        min(
            limit,
            reference_power_mw(
                configuration,
                datetime.fromisoformat(row["start"]),
                datetime.fromisoformat(row["end"]),
                row["power_kw"],
                model_version=default,
            ),
        )
        for row in snapshot.normalized_intervals
    ]
    history(system, plant)
    run = request_run(system, plant)
    assert run_calibration(run.pk, NOW)
    run.refresh_from_db()
    assert run.result["transfer_model"] == default
    pieces = [
        piece
        for observation in run.dataset_manifest["observations"]
        for piece in observation["intervals"]
    ]
    assert pieces
    assert [piece["power_mw"] for piece in pieces] == [
        reference_power_mw(
            configuration,
            datetime.fromisoformat(piece["start"]),
            datetime.fromisoformat(piece["end"]),
            0.4,
            model_version=default,
        )
        for piece in pieces
    ]


def test_the_two_versions_give_tracking_plants_different_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, _, configuration = _tracking_plant(monkeypatch)
    snapshot = snapshot_for(system, DAY, collected_at=NOW)
    payloads = {}
    for default in VERSIONS:
        _default(monkeypatch, default)
        payloads[default] = forecast_payload(configuration, snapshot, None, DAY, 30)
    assert _powers(payloads[MODEL_V1]) != _powers(payloads[MODEL_V2])
    assert payloads[MODEL_V1]["model_version"] == MODEL_V1
    assert payloads[MODEL_V2]["model_version"] == MODEL_V2


def test_a_stored_v1_issuance_and_its_outage_scenario_replay_unchanged_under_the_v2_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, _ = _tracking_plant(monkeypatch)
    _default(monkeypatch, MODEL_V1)
    incident(system, plant, NOW - timedelta(hours=1), None)
    publish_snapshot(snapshot_for(system, DAY, collected_at=NOW))
    stored = _outlook(system, plant, DAY, as_of=NOW)
    normal = cast(dict[str, Any], stored["result"])
    scenario = cast(dict[str, Any], stored["outage_scenario"])["result"]
    assert normal["model_version"] == scenario["model_version"] == MODEL_V1
    _default(monkeypatch, MODEL_V2)
    assert _outlook(system, plant, DAY, as_of=NOW) == stored
    assert ForecastIssuance.objects.get().model_version == MODEL_V1


def test_an_outage_scenario_replays_the_version_recorded_in_its_forecast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, configuration = _tracking_plant(monkeypatch)
    _default(monkeypatch, MODEL_V1)
    first = _calibration(system, plant, MODEL_V1)
    _activate(system, plant, first)
    incident(system, plant, NOW - timedelta(hours=1), None)
    snapshot = snapshot_for(system, DAY, collected_at=NOW)

    def scenario() -> dict[str, Any]:
        outlook = {
            "plant_id": plant.pk,
            "configuration_id": configuration.pk,
            "calibration_id": first.pk,
            "result": forecast_payload(configuration, snapshot, first, DAY, 60),
        }
        return cast(dict[str, Any], incident_scenario(outlook, DAY, 60, NOW))

    under_v1 = scenario()
    _default(monkeypatch, MODEL_V2)
    assert scenario() == under_v1
    assert under_v1["result"]["model_version"] == MODEL_V1
    assert under_v1["status"] == "estimated"


def test_a_v1_calibration_keeps_its_plant_on_v1_until_a_refit_under_v2_is_activated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, configuration = _tracking_plant(monkeypatch)
    _default(monkeypatch, MODEL_V1)
    first = _calibration(system, plant, MODEL_V1)
    _activate(system, plant, first)
    snapshot = publish_snapshot(snapshot_for(system, DAY, collected_at=NOW))
    under_v1 = forecast_payload(configuration, snapshot, first, DAY, 30)
    issuance = ForecastIssuance.objects.get()
    assert (issuance.calibration_id, issuance.model_version, issuance.factor) == (
        first.pk,
        MODEL_V1,
        0.5,
    )
    _default(monkeypatch, MODEL_V2)
    assert forecast_payload(configuration, snapshot, first, DAY, 30) == under_v1
    assert under_v1["model_version"] == MODEL_V1
    assert forecast_payload(configuration, snapshot, None, DAY, 30)["model_version"] == MODEL_V2
    second = _calibration(system, plant, MODEL_V2)
    _activate(system, plant, second)
    refit = forecast_payload(configuration, snapshot, second, DAY, 30)
    _default(monkeypatch, MODEL_V1)
    assert forecast_payload(configuration, snapshot, second, DAY, 30) == refit
    assert refit["model_version"] == MODEL_V2
    assert _powers(refit) != _powers(under_v1)


@pytest.mark.parametrize(("default", "incumbent"), [(MODEL_V1, 0.5), (MODEL_V2, 1.0)])
def test_a_refit_is_compared_only_with_an_incumbent_fitted_under_its_own_version(
    monkeypatch: pytest.MonkeyPatch, default: str, incumbent: float
) -> None:
    system, plant, configuration = _tracking_plant(monkeypatch)
    history(system, plant)
    first = _calibration(system, plant, MODEL_V1)
    _activate(system, plant, first)
    _default(monkeypatch, default)
    refit = request_run(system, plant)
    dataset = build_calibration_dataset(refit, configuration)
    assert dataset.transfer_model == default
    assert dataset.incumbent_factor == incumbent
    assert dataset.incumbent_is_active is (default == MODEL_V1)


def test_fixed_plants_are_identical_under_both_default_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration = _configuration(plant)
    history(system, plant)
    snapshot = snapshot_for(system, DAY, collected_at=NOW)
    results = []
    for default in VERSIONS:
        _default(monkeypatch, default)
        run = request_run(system, plant)
        assert run_calibration(run.pk, NOW)
        run.refresh_from_db()
        payload = forecast_payload(configuration, snapshot, None, DAY, 30)
        assert payload["model_version"] == run.result["transfer_model"] == MODEL_VERSION
        results.append(
            (
                run.status,
                run.factor,
                run.result,
                run.dataset_manifest,
                run.dataset_sha256,
                payload,
            )
        )
    assert results[0] == results[1]


def test_production_diagnostic_needs_evidence_fitted_under_the_forecast_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, _ = _tracking_plant(monkeypatch)
    PlantConfiguration.objects.filter(plant=plant).update(created_at=NOW)
    _default(monkeypatch, MODEL_V1)
    first = _calibration(system, plant, MODEL_V1)
    _activate(system, plant, first)
    publish_snapshot(snapshot_for(system, NOW.date(), collected_at=NOW))
    evidence = {
        "policy": DERATING_POLICY,
        "status": "suspected",
        "baseline_factor": 0.5,
        "latest_matched_day": "2026-11-08",
    }
    first.result["derating"] = evidence
    first.save(update_fields=("result",))
    _default(monkeypatch, MODEL_V2)
    outlook = _outlook(system, plant, NOW.date())
    assert cast(dict[str, Any], outlook["result"])["model_version"] == MODEL_V1
    assert cast(dict[str, Any], outlook["production_diagnostic"])["calibration_id"] == first.pk
    other = _calibration(system, plant, MODEL_V2)
    other.result["derating"] = evidence
    other.save(update_fields=("result",))
    assert _outlook(system, plant, NOW.date())["production_diagnostic"] is None


@pytest.mark.parametrize("calibrated", VERSIONS)
def test_a_recomputed_performance_day_uses_the_version_of_its_calibration(
    monkeypatch: pytest.MonkeyPatch, calibrated: str
) -> None:
    system, plant, configuration = _tracking_plant(monkeypatch)
    reference = actual_snapshot_for(system, MEASURED_DAY, power_kw=0.2)
    connection = EpiasConnection.objects.create(
        plant=plant,
        provider_plant_id=2,
        settlement_plant_id=77,
        eic="EIC",
        name="Plant",
        connected_by=system.member,
    )
    acquisition((configuration, connection))
    selected_factor(system, configuration, NOW - timedelta(days=20))
    CalibrationRun.objects.update(
        result={"training_day_dates": [MEASURED_DAY.isoformat()], "transfer_model": calibrated}
    )
    limit = power_limit(configuration)
    assert limit is not None
    expected = sum(
        min(
            limit,
            0.8
            * reference_power_mw(
                configuration,
                datetime.fromisoformat(row["start"]),
                datetime.fromisoformat(row["end"]),
                row["power_kw"],
                model_version=calibrated,
            ),
        )
        / 2
        for row in reference.normalized_intervals
    )
    for default in VERSIONS:
        _default(monkeypatch, default)
        day = forecast_performance(
            plant,
            start_date=MEASURED_DAY,
            end_date=MEASURED_DAY,
            comparison="historical_model",
        )["days"][0]
        assert day["factor"] == 0.8
        assert day["model_version"] == calibrated
        assert day["predicted_energy_mwh"] == pytest.approx(expected, rel=1e-12)
