from dataclasses import asdict
from datetime import date, timedelta
from typing import Any

import pytest

from pyracast.forecasts.models import HistoricalReferenceImport
from pyracast.irradiance import local_day_bounds
from pyracast.plants.automation import set_calibration_automation
from pyracast.plants.calibration_data import current_source_validation
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    PlantConfiguration,
    PlantEvent,
)
from pyracast.plants.operations import ObservationInput, import_observations
from pyracast.plants.training import run_calibration
from tests.test_domain import NOW
from tests.test_historical_reference_import import source_settings
from tests.test_plant_operations import make_plant
from tests.test_plant_training import actual_snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    "source_patch",
    [
        {"snapshot_id": None},
        {"snapshot_id": True},
        {"snapshot_id": 0},
        {"binding_id": True},
        {"binding_id": 2},
        {"source_kind": "forecast"},
        {"source_product": "unknown"},
        {"reference_import": {"id": 1}},
    ],
)
def test_only_explicit_native_snapshot_provenance_counts_toward_current_source_support(
    source_patch: dict[str, Any],
) -> None:
    days = [(date(2026, 10, 1) + timedelta(days=offset)).isoformat() for offset in range(30)]
    source = {
        "snapshot_id": 1,
        "binding_id": 1,
        "source_kind": "estimated_actuals",
        "source_product": "rooftop_pv_power",
        "reference_import": None,
    }
    manifest: dict[str, object] = {
        "binding_id": 1,
        "sources": {day: source | source_patch for day in days},
    }
    report: dict[str, object] = {
        "valid_days": 30,
        "included_day_dates": days,
        "training_days": 23,
        "training_day_dates": days[:-7],
        "holdout_days": 7,
        "holdout_day_dates": days[-7:],
    }
    result = current_source_validation(manifest, report, 1)
    assert result["valid_days"] == 0
    assert result["holdout_days"] == 0
    assert result["eligible"] is False


def _mixed_report(
    training: list[str], holdout: list[str], products: dict[str, str]
) -> tuple[dict[str, object], dict[str, object]]:
    days = sorted(products)
    sources = {
        day: {
            "snapshot_id": index + 1,
            "binding_id": 1,
            "source_kind": "estimated_actuals",
            "source_product": products[day],
            "reference_import": None if products[day] == "rooftop_pv_power" else {"id": 1},
        }
        for index, day in enumerate(days)
    }
    report: dict[str, object] = {
        "included_day_dates": days,
        "valid_days": len(days),
        "training_day_dates": training,
        "training_days": len(training),
        "holdout_day_dates": holdout,
        "holdout_days": len(holdout),
    }
    return {"binding_id": 1, "sources": sources}, report


def test_settings_matched_history_supports_training_behind_a_native_holdout() -> None:
    days = [(date(2026, 9, 1) + timedelta(days=offset)).isoformat() for offset in range(90)]
    products = dict.fromkeys(days, "historical_rooftop_pv") | dict.fromkeys(
        days[:30] + days[-7:], "rooftop_pv_power"
    )
    manifest, report = _mixed_report(days[-37:-7], days[-7:], products)
    summary = current_source_validation(manifest, report, 1)
    assert summary["policy"] == "native-final-7-settings-matched-selected-30-v3"
    assert summary["valid_day_dates"] == days[-7:]
    assert summary["valid_days"] == 7
    assert summary["historical_valid_days"] == 30
    assert summary["training_days"] == 0
    assert summary["holdout_days"] == 7
    assert summary["eligible"] is True


@pytest.mark.parametrize(
    ("training_days", "patch"),
    [
        (22, {}),
        (30, {-1: "historical_rooftop_pv"}),
        (30, {-10: "unknown"}),
    ],
)
def test_promotion_evidence_needs_thirty_supported_days_and_a_native_holdout(
    training_days: int, patch: dict[int, str]
) -> None:
    days = [(date(2026, 9, 1) + timedelta(days=offset)).isoformat() for offset in range(90)]
    products = dict.fromkeys(days, "historical_rooftop_pv") | dict.fromkeys(
        days[-7:], "rooftop_pv_power"
    )
    for offset, product in patch.items():
        products[days[offset]] = product
    manifest, report = _mixed_report(days[-7 - training_days : -7], days[-7:], products)
    assert current_source_validation(manifest, report, 1)["eligible"] is False


@pytest.mark.parametrize("imported_days", ["all", "one_holdout_day", "none"])
def test_real_fit_distinguishes_imported_product_from_current_hobby_validation(
    monkeypatch: pytest.MonkeyPatch,
    imported_days: str,
) -> None:
    system, plant = make_plant(monkeypatch)
    assert plant.current_configuration_id is not None
    PlantConfiguration.objects.filter(pk=plant.current_configuration_id).update(created_at=NOW)
    set_calibration_automation(
        system.member, system.tenant.pk, system.region.pk, plant.pk, enabled=True
    )
    imported = HistoricalReferenceImport.objects.create(
        binding=system.binding,
        sha256="a" * 64,
        source_name="authorized-export.json",
        source_settings=asdict(source_settings()),
        imported_by=system.admin,
        imported_at=NOW,
        interval_count=37 * 48 if imported_days == "all" else 48,
    )
    observations = []
    for offset in range(37):
        day = date(2026, 10, 1) + timedelta(days=offset)
        snapshot = actual_snapshot_for(system, day)
        if imported_days == "all" or (imported_days == "one_holdout_day" and offset == 36):
            snapshot.job.reference_import = imported
            snapshot.job.endpoint = "historical_import"
            snapshot.job.scheduled_slot = NOW
            snapshot.job.save()
            snapshot.collected_at = snapshot.available_at = NOW
            snapshot.save()
        start, end = local_day_bounds(day, system.region.timezone)
        while start < end:
            observations.append(ObservationInput(start, start + timedelta(hours=1), 26))
            start += timedelta(hours=1)
    import_observations(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        configuration_id=plant.current_configuration_id,
        unit="MWh",
        observations=observations,
    )
    run = CalibrationRun.objects.get()
    assert run_calibration(run.pk, NOW)
    run.refresh_from_db()
    assert run.status == "ready"
    assert run.assessment == "evaluated"
    selected_days = 36 if imported_days == "one_holdout_day" else 37
    assert run.result["valid_days"] == selected_days
    validation = run.result["current_source_validation"]
    assert validation["reference_basis"] == "modeled_estimated_actuals_not_measurements"
    assert run.dataset_manifest["configuration_declared_at"] == NOW.isoformat()
    assert (
        run.dataset_manifest["configuration_time_policy"]
        == "current-declaration-applied-retrospectively-v1"
    )
    assert run.dataset_manifest["retrospective_configuration_days"] == selected_days
    cohort = run.dataset_manifest["reference_cohort"]
    assert cohort["selected_product"] == (
        "historical_rooftop_pv" if imported_days == "all" else "rooftop_pv_power"
    )
    assert cohort["excluded_samples"] == (24 if imported_days == "one_holdout_day" else 0)
    assert len(run.dataset_manifest["observations"]) == 37 * 24
    if imported_days != "all":
        assert validation["valid_days"] == selected_days
        assert validation["holdout_days"] == 7
        assert validation["eligible"]
        assert CalibrationActivation.objects.get().calibration_id == run.pk
    else:
        assert validation["valid_days"] == 0
        assert validation["holdout_days"] == 0
        assert not validation["eligible"]
        assert not CalibrationActivation.objects.exists()
        assert (
            PlantEvent.objects.latest("pk").details["reason"]
            == "insufficient_current_source_validation"
        )
    source = run.dataset_manifest["sources"]["2026-11-06"]
    if imported_days == "none":
        assert source["source_product"] == "rooftop_pv_power"
        assert source["reference_import"] is None
    else:
        assert source["source_product"] == "historical_rooftop_pv"
        assert source["reference_import"] == {
            "id": imported.pk,
            "sha256": imported.sha256,
            "source_name": "authorized-export.json",
            "source_settings": imported.source_settings,
            "imported_at": NOW.isoformat(),
        }
