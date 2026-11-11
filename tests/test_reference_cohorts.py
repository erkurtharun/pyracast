from datetime import date, timedelta

import pytest

from pyracast.forecasts.models import HistoricalReferenceImport
from pyracast.irradiance import local_day_bounds
from pyracast.plants.models import CalibrationRun, Plant, ProductionObservation
from pyracast.plants.preparation import prepare_calibration
from pyracast.plants.training import run_calibration
from tests.test_domain import NOW, System
from tests.test_plant_operations import make_plant
from tests.test_plant_training import actual_snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)


def prepare_cohorts(
    monkeypatch: pytest.MonkeyPatch,
    *,
    native: int,
    historical: int,
    unknown: int = 0,
    native_power: float = 0.4,
    invalid_native_day: bool = False,
) -> tuple[System, Plant, CalibrationRun]:
    system, plant = make_plant(monkeypatch)
    assert plant.current_configuration_id is not None
    imported = HistoricalReferenceImport.objects.create(
        binding=system.binding,
        sha256="a" * 64,
        source_name="authorized-export.json",
        source_settings={"product": "historical_rooftop_pv"},
        imported_by=system.admin,
        imported_at=NOW,
        interval_count=historical * 48,
    )
    observations = []
    for offset in range(historical + unknown + native):
        day = date(2026, 7, 1) + timedelta(days=offset)
        current = offset >= historical + unknown
        snapshot = actual_snapshot_for(system, day, power_kw=native_power if current else 0.4)
        if offset < historical:
            snapshot.job.reference_import = imported
            snapshot.job.endpoint = "historical_import"
            snapshot.job.save()
        elif not current:
            snapshot.job.endpoint = "legacy_unknown"
            snapshot.job.save()
        start, end = local_day_bounds(day, system.binding.timezone)
        cursor = start
        while cursor < end:
            invalid = invalid_native_day and current and offset == historical + unknown
            quality = "invalid" if invalid and cursor == start + timedelta(hours=12) else "valid"
            observations.append(
                ProductionObservation(
                    plant=plant,
                    configuration_id=plant.current_configuration_id,
                    starts_at=cursor,
                    ends_at=cursor + timedelta(hours=1),
                    energy_mwh=26,
                    quality=quality,
                    source="meter",
                    recorded_by=system.member,
                )
            )
            cursor += timedelta(hours=1)
    ProductionObservation.objects.bulk_create(observations)
    run = prepare_calibration(plant.pk)
    assert run is not None
    return system, plant, run


@pytest.mark.parametrize(
    ("native", "historical", "unknown", "product", "valid_days", "status"),
    [
        (6, 6, 0, "rooftop_pv_power", 6, "insufficient"),
        (2, 5, 2, "historical_rooftop_pv", 5, "insufficient"),
        (6, 7, 0, "historical_rooftop_pv", 7, "ready"),
        (1, 6, 7, "unknown", 7, "ready"),
        (0, 0, 7, "unknown", 7, "ready"),
        (0, 7, 0, "historical_rooftop_pv", 7, "ready"),
        (30, 60, 0, "rooftop_pv_power", 30, "ready"),
    ],
)
def test_fit_never_combines_products_to_reach_minimum_or_enlarge_a_current_cohort(
    monkeypatch: pytest.MonkeyPatch,
    native: int,
    historical: int,
    unknown: int,
    product: str,
    valid_days: int,
    status: str,
) -> None:
    _, _, run = prepare_cohorts(monkeypatch, native=native, historical=historical, unknown=unknown)
    assert run_calibration(run.pk, NOW)
    run.refresh_from_db()
    assert run.status == status
    assert run.result["valid_days"] == valid_days
    cohort = run.dataset_manifest["reference_cohort"]
    assert cohort["selected_product"] == product
    assert cohort["cohorts"]["rooftop_pv_power"]["complete_matched_days"] == native
    assert cohort["cohorts"]["historical_rooftop_pv"]["complete_matched_days"] == historical
    assert cohort["cohorts"]["unknown"]["complete_matched_days"] == unknown
    assert cohort["selected_samples"] == valid_days * 24
    assert cohort["excluded_samples"] == (native + historical + unknown - valid_days) * 24
    assert len(run.dataset_manifest["observations"]) == (native + historical + unknown) * 24
    assert len(run.dataset_manifest["sources"]) == native + historical + unknown
    assert run.reference_snapshot_ids is not None
    assert len(run.reference_snapshot_ids) == native + historical + unknown
    assert run.result["selection"]["reference_cohort"] == cohort
    assert run.result["selection"]["selected_observations"] == valid_days * 24
    assert run.result["current_source_validation"]["eligible"] == (native == 30)


def test_native_priority_does_not_choose_a_product_using_candidate_holdout_performance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, run = prepare_cohorts(monkeypatch, native=7, historical=15, native_power=0.2)
    assert run_calibration(run.pk, NOW)
    run.refresh_from_db()
    assert run.dataset_manifest["reference_cohort"]["selected_product"] == "rooftop_pv_power"
    assert run.result["valid_days"] == 7
    assert run.status == "rejected"
    assert "baseline_improvement_below_margin" in run.result["reasons"]


def test_cohort_minimum_uses_complete_valid_matched_daylight_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, run = prepare_cohorts(monkeypatch, native=7, historical=7, invalid_native_day=True)
    assert run_calibration(run.pk, NOW)
    run.refresh_from_db()
    cohort = run.dataset_manifest["reference_cohort"]
    assert cohort["cohorts"]["rooftop_pv_power"]["complete_matched_days"] == 6
    assert cohort["selected_product"] == "historical_rooftop_pv"
    assert run.result["valid_days"] == 7
    assert not run.result["current_source_validation"]["eligible"]
