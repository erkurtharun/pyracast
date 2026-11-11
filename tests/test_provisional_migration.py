from datetime import date

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from pyracast.plants.measurement_policy import LEGACY_MEASUREMENT_POLICY, MEASUREMENT_POLICY
from pyracast.plants.models import CalibrationRun
from tests.test_domain import NOW
from tests.test_plant_operations import make_plant

pytestmark = pytest.mark.django_db(transaction=True)


def test_existing_runs_keep_settlement_pins_and_policy_when_provisional_support_is_added(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    old_target = [("plants", "0007_calibration_incident_evidence")]
    executor = MigrationExecutor(connection)
    current_targets = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate(old_target)
        old_run = executor.loader.project_state(old_target).apps.get_model(
            "plants", "CalibrationRun"
        )
        prior = old_run.objects.create(
            plant_id=plant.pk,
            configuration_id=plant.current_configuration_id,
            binding_id=system.binding.pk,
            requested_by_id=system.member.pk,
            window_start=date(2026, 11, 1),
            window_end=date(2026, 11, 7),
            cutoff_at=NOW,
            settlement_snapshot_ids=[41, 42],
            status="ready",
            factor=0.8,
            dataset_manifest={"measurement_selection_policy": LEGACY_MEASUREMENT_POLICY},
            dataset_sha256="frozen-evidence",
        )
    finally:
        MigrationExecutor(connection).migrate(current_targets)
    restored = CalibrationRun.objects.get(pk=prior.pk)
    assert restored.production_snapshot_ids == [41, 42]
    assert restored.measurement_selection_policy == LEGACY_MEASUREMENT_POLICY
    assert restored.factor == 0.8
    assert restored.dataset_manifest == {"measurement_selection_policy": LEGACY_MEASUREMENT_POLICY}
    assert restored.dataset_sha256 == "frozen-evidence"
    assert CalibrationRun().measurement_selection_policy == MEASUREMENT_POLICY
    plant.refresh_from_db()
    assert plant.automatic_calibration_enabled is False
