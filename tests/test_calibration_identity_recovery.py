from datetime import date, timedelta
from math import ceil, nextafter

import pytest
from django.db import transaction

from pyracast.irradiance import local_day_bounds
from pyracast.plants.activation import activate_calibration
from pyracast.plants.automation import (
    lock_enabler,
    promote_automatic_calibration,
    set_calibration_automation,
)
from pyracast.plants.models import (
    CalibrationActivation,
    Plant,
    PlantEvent,
    ProductionObservation,
)
from pyracast.plants.preparation import prepare_calibration
from pyracast.plants.training import run_calibration
from pyracast.regions.models import Region
from tests.support.domain import NOW
from tests.support.plant_operations import make_plant, ready_run
from tests.support.plant_training import actual_snapshot_for

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("recovered", None),
        ("overlapping_holdout", "awaiting_new_holdout_days"),
        ("stale", "stale_automatic_evaluation"),
        ("short_history", "insufficient_automatic_evaluation"),
        ("zero_training", "candidate_insufficient"),
        ("zero_holdout", "candidate_rejected"),
        ("high_recovered", None),
        ("high_incumbent_zero_holdout", "candidate_rejected"),
        ("suspected_huber", "derating_suspected"),
        ("forged_identity_kind", "derating_suspected"),
    ],
)
def test_durable_identity_recovery_obeys_promotion_and_observation_guards(
    monkeypatch: pytest.MonkeyPatch, case: str, expected_reason: str | None
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration = plant.current_configuration
    assert configuration is not None
    days = 29 if case == "short_history" else 30
    last = date(2026, 10, 20) if case == "stale" else date(2026, 11, 8)
    first = last - timedelta(days=days - 1)
    incumbent = ready_run(system, plant)
    high = case in {
        "high_recovered",
        "high_incumbent_zero_holdout",
        "suspected_huber",
        "forged_identity_kind",
    }
    incumbent.factor = 1.5 if case == "high_incumbent_zero_holdout" else 1.3 if high else 0.5
    incumbent.window_end = (
        last - timedelta(days=5) if case == "overlapping_holdout" else first - timedelta(days=1)
    )
    incumbent.window_start = incumbent.window_end - timedelta(days=6)
    incumbent.result["holdout_day_dates"] = [
        (incumbent.window_end - timedelta(days=1)).isoformat(),
        incumbent.window_end.isoformat(),
    ]
    incumbent.save()
    activation = activate_calibration(
        system.member, system.tenant.pk, system.region.pk, plant.pk, incumbent.pk
    )
    holdout_days = 7 if days >= 30 else max(2, ceil(days * 0.2))
    observations = []
    for offset in range(days):
        day = first + timedelta(days=offset)
        actual_snapshot_for(system, day, power_kw=0.4 if high else 0.6)
        training = offset < days - holdout_days
        energy: float = (
            0
            if (training and case == "zero_training") or (not training and case == "zero_holdout")
            else 39
            if training
            else 78
        )
        if case == "high_recovered":
            energy = 52
        elif case == "high_incumbent_zero_holdout":
            energy = 0 if offset >= 26 else 78
        elif case in {"suspected_huber", "forged_identity_kind"}:
            energy = 41.6
        start, end = local_day_bounds(day, system.binding.timezone)
        while start < end:
            observations.append(
                ProductionObservation(
                    plant=plant,
                    configuration=configuration,
                    starts_at=start,
                    ends_at=start + timedelta(hours=1),
                    energy_mwh=energy,
                    source="meter",
                    recorded_by=system.member,
                )
            )
            start += timedelta(hours=1)
    ProductionObservation.objects.bulk_create(observations)
    candidate = prepare_calibration(plant.pk, NOW)
    assert candidate is not None
    assert candidate.comparison_activation_id == activation.pk
    assert run_calibration(candidate.pk, NOW)
    candidate.refresh_from_db()
    assert candidate.result["valid_days"] == days
    assert candidate.dataset_manifest["incumbent_factor"] == incumbent.factor
    assert candidate.result["derating"]["reference_kind"] == "active_calibration"
    assert candidate.result["derating"]["latest_matched_day"] == last.isoformat()
    if case == "zero_training":
        assert candidate.status == "insufficient"
        assert candidate.result["training_support"]["informative_days"] == 0
        assert (
            candidate.result["exclusions"]["zero_production_training_days"] == days - holdout_days
        )
    elif case in {"zero_holdout", "high_incumbent_zero_holdout"}:
        assert candidate.status == "rejected"
        assert "zero_production_holdout" in candidate.result["reasons"]
        assert candidate.result["derating"]["latest_performance_index"] == 0
        assert candidate.result["derating"]["days"][-1]["observed_zero"] is True
    elif case in {"suspected_huber", "forged_identity_kind"}:
        assert candidate.status == "ready"
        assert candidate.factor is not None
        assert 0.8 < candidate.factor < 0.81
        assert candidate.result["candidate_kind"] == "huber"
        assert candidate.result["derating"]["status"] == "suspected"
    else:
        assert candidate.status == "ready"
        assert candidate.factor == 1
        assert candidate.result["candidate_kind"] == "identity_recovery"
        if high:
            assert candidate.result["fitted_factor"] == pytest.approx(1)
        else:
            assert candidate.result["fitted_factor"] < 0.51
        assert candidate.result["candidate"] == candidate.result["baseline"]
        assert candidate.result["candidate"]["daily_mae_mwh"] == pytest.approx(0, abs=1e-9)
        assert candidate.result["incumbent"]["daily_mae_mwh"] > 0
        assert set(candidate.result["training_day_dates"]).isdisjoint(
            candidate.result["holdout_day_dates"]
        )
    selected = CalibrationActivation.objects.latest("created_at", "pk")
    if expected_reason is None:
        assert selected.calibration_id == candidate.pk
        assert selected.actor_id is None
        assert CalibrationActivation.objects.count() == 2
    else:
        assert selected.pk == activation.pk
        skipped = PlantEvent.objects.filter(action="calibration_automatic_skipped").latest("pk")
        assert skipped.details["reason"] == expected_reason
        assert CalibrationActivation.objects.count() == 1
    if case == "forged_identity_kind":
        candidate.result["candidate_kind"] = "identity_recovery"
        assert candidate.factor is not None
        assert candidate.completed_at is not None
        for forged_factor in (candidate.factor, nextafter(1.0, 2.0)):
            candidate.factor = forged_factor
            candidate.save(update_fields=("factor", "result"))
            with transaction.atomic():
                enabler = lock_enabler(plant.pk)
                Region.objects.select_for_update().get(pk=plant.region_id)
                locked = Plant.objects.select_for_update().get(pk=plant.pk)
                assert not promote_automatic_calibration(
                    locked, candidate, candidate.completed_at + timedelta(seconds=1), enabler
                )
            assert CalibrationActivation.objects.count() == 1
            assert CalibrationActivation.objects.get().calibration_id == incumbent.pk
            skipped = PlantEvent.objects.filter(action="calibration_automatic_skipped").latest("pk")
            assert skipped.details["reason"] == "derating_suspected"


def test_zero_only_holdout_cannot_validate_first_automatic_calibration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration = plant.current_configuration
    assert configuration is not None
    set_calibration_automation(
        system.member, system.tenant.pk, system.region.pk, plant.pk, enabled=True
    )
    observations = []
    for offset in range(30):
        day = date(2026, 10, 10) + timedelta(days=offset)
        actual_snapshot_for(system, day, power_kw=0.6)
        start, end = local_day_bounds(day, system.binding.timezone)
        while start < end:
            observations.append(
                ProductionObservation(
                    plant=plant,
                    configuration=configuration,
                    starts_at=start,
                    ends_at=start + timedelta(hours=1),
                    energy_mwh=39 if offset < 23 else 0,
                    source="meter",
                    recorded_by=system.member,
                )
            )
            start += timedelta(hours=1)
    ProductionObservation.objects.bulk_create(observations)
    candidate = prepare_calibration(plant.pk, NOW)
    assert candidate is not None
    assert run_calibration(candidate.pk, NOW)
    candidate.refresh_from_db()
    assert candidate.status == "rejected"
    assert candidate.factor is None
    assert "zero_production_holdout" in candidate.result["reasons"]
    assert candidate.result["valid_days"] == 30
    assert candidate.result["holdout_days"] == 7
    assert candidate.result["training_support"]["sufficient"] is True
    assert candidate.result["current_source_validation"]["eligible"] is True
    assert (
        candidate.result["candidate"]["daily_mae_mwh"]
        < candidate.result["baseline"]["daily_mae_mwh"]
    )
    assert candidate.result["derating"]["days"][-1]["observed_zero"] is True
    assert not CalibrationActivation.objects.exists()
