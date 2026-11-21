from datetime import UTC, datetime, timedelta

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from pyracast.plants.models import CalibrationRun, PlantConfiguration
from tests.support.delivery_health import NOW
from tests.support.migrations import forward, migrate
from tests.support.plant_operations import make_plant

pytestmark = pytest.mark.django_db(transaction=True)

PREVIOUS = ("plants", "0006_submission_updated_at")


def test_a_requeued_run_waits_from_the_upgrade_and_every_other_run_from_its_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration = PlantConfiguration.objects.get(plant=plant)
    migrate(PREVIOUS)
    try:
        apps = MigrationExecutor(connection).loader.project_state([PREVIOUS]).apps
        runs = apps.get_model("plants", "CalibrationRun")._default_manager
        common = {
            "plant_id": plant.pk,
            "configuration_id": configuration.pk,
            "binding_id": system.binding.pk,
            "requested_by_id": system.member.pk,
            "window_start": NOW.date() - timedelta(days=7),
            "window_end": NOW.date() - timedelta(days=1),
            "cutoff_at": NOW - timedelta(days=1),
        }
        requeued = runs.create(**common, status="pending", attempts=1).pk
        finished = runs.create(
            **common, status="ready", attempts=1, completed_at=NOW - timedelta(hours=1)
        ).pk
        replaced = runs.create(**common, status="superseded", completed_at=NOW).pk
        runs.filter(pk__in=(requeued, finished, replaced)).update(
            created_at=NOW - timedelta(days=2)
        )
        before = datetime.now(UTC)
    finally:
        forward()

    waits = dict(CalibrationRun.objects.values_list("pk", "available_at"))
    assert waits[finished] == waits[replaced] == NOW - timedelta(days=2)
    requeued_wait = waits[requeued]
    assert requeued_wait is not None
    assert before <= requeued_wait <= datetime.now(UTC)
