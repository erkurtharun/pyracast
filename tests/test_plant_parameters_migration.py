import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from pyracast.plants.models import Plant, PlantSubmission
from pyracast.plants.parameters import declaration
from pyracast.regions.models import Region
from tests.support.domain import NOW
from tests.support.migrations import forward, migrate
from tests.support.plant_operations import make_plant
from tests.support.trash_operations import sibling

pytestmark = pytest.mark.django_db(transaction=True)

PREVIOUS = (
    ("plants", "0006_submission_updated_at"),
    ("regions", "0009_audit_connection_validation_failed"),
)


def test_an_upgrade_lists_a_started_purge_and_keeps_every_plant_on_its_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant = make_plant(monkeypatch)
    PlantSubmission.objects.create(plant=plant, submitted_by=system.member, dc_capacity_mw=1)
    waiting, _ = sibling(system)
    migrate(*PREVIOUS)
    try:
        apps = MigrationExecutor(connection).loader.project_state(list(PREVIOUS)).apps
        regions = apps.get_model("regions", "Region")._default_manager
        regions.filter(pk=system.region.pk).update(deleted_at=NOW, purge_started_at=NOW)
        regions.filter(pk=waiting.pk).update(deleted_at=NOW)
    finally:
        forward()

    found = {
        pk: (plant_ids, bindings)
        for pk, plant_ids, bindings in Region.objects.values_list(
            "pk", "purge_plant_ids", "purge_bindings"
        )
    }
    assert found[system.region.pk] == ([plant.pk], [f"fixture:{system.account.pk}:site"])
    assert found[waiting.pk] == (None, None)
    stored = Plant.objects.select_related("submission", "current_configuration").get(pk=plant.pk)
    assert stored.parameters_revision == 1
    assert stored.submission.base_configuration_id is None
    assert declaration(stored) == stored.current_configuration
