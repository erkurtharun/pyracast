import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client

from pyracast.connections.operations import rotate_secret
from pyracast.plants.models import Plant, PlantConfiguration, PlantEvent, PlantSubmission
from pyracast.regions.models import Region
from tests.support.domain import NOW
from tests.support.plant_operations import make_plant

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("change", ["binding_removed", "approval_revoked", "credential_rotated"])
def test_revising_an_approved_plant_requires_a_current_validated_binding_and_keeps_history(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration_id = plant.current_configuration_id
    assert configuration_id is not None
    previous_configuration = PlantConfiguration.objects.values().get(pk=configuration_id)
    events = list(PlantEvent.objects.filter(plant=plant).values_list("pk", flat=True))
    if change == "binding_removed":
        Region.objects.filter(pk=system.region.pk).update(approved_binding=None)
    elif change == "approval_revoked":
        Region.objects.filter(pk=system.region.pk).update(status="suspended")
    else:
        rotate_secret(system.admin, system.account.pk, "test-only-rotated-key")

    client = Client()
    client.force_login(system.member)
    response = client.put(
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/",
        {"name": "Changed", "dc_capacity_mw": 150},
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.json() == {
        "code": "invalid_request",
        "detail": ["An approved current provider binding is required."],
    }
    plant.refresh_from_db()
    assert plant.name == "Plant"
    assert plant.current_configuration_id == previous_configuration["id"]
    assert PlantConfiguration.objects.values().get(pk=configuration_id) == (previous_configuration)
    assert PlantConfiguration.objects.count() == 1
    assert list(PlantEvent.objects.filter(plant=plant).values_list("pk", flat=True)) == events
    assert not PlantSubmission.objects.exists()


@pytest.mark.parametrize("kind", ["plants", "regions"])
def test_onboarding_migration_refuses_legacy_conflicts_without_removing_records(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    system, plant = make_plant(monkeypatch)
    configuration_id = plant.current_configuration_id
    assert configuration_id is not None
    previous_configuration = PlantConfiguration.objects.values().get(pk=configuration_id)
    executor = MigrationExecutor(connection)
    current_targets = executor.loader.graph.leaf_nodes()
    old_target = [
        (
            kind,
            "0009_calibration_automation"
            if kind == "plants"
            else "0004_regionlocationchangerequest",
        )
    ]
    old_model = executor.loader.project_state(old_target).apps.get_model(
        kind, "Plant" if kind == "plants" else "Region"
    )
    duplicate_id: int | None = None
    try:
        executor.migrate(old_target)
        legacy_loader = MigrationExecutor(connection).loader
        legacy_apps = legacy_loader.project_state(list(legacy_loader.applied_migrations)).apps
        legacy_configuration = legacy_apps.get_model("plants", "PlantConfiguration")
        previous_legacy_configuration = legacy_configuration.objects.values().get(
            pk=configuration_id
        )
        if kind == "plants":
            duplicate = old_model.objects.create(
                region_id=system.region.pk, name="Legacy second plant"
            )
            error = "Multiple active plants"
            original_id = plant.pk
        else:
            duplicate = old_model.objects.create(
                tenant_id=system.tenant.pk,
                name="Legacy second location",
                latitude=40.0000001,
                longitude=29.0000001,
                timezone="UTC",
            )
            error = "Duplicate active tenant locations"
            original_id = system.region.pk
        duplicate_id = duplicate.pk
        before = list(old_model.objects.order_by("pk").values())

        with pytest.raises(RuntimeError, match=error):
            MigrationExecutor(connection).migrate(current_targets)

        assert list(old_model.objects.order_by("pk").values()) == before
        assert old_model.objects.filter(pk__in=(original_id, duplicate_id)).count() == 2
        assert legacy_configuration.objects.values().get(pk=configuration_id) == (
            previous_legacy_configuration
        )
    finally:
        if duplicate_id is not None:
            old_model.objects.filter(pk=duplicate_id).update(deleted_at=NOW)
        MigrationExecutor(connection).migrate(current_targets)

    current_model = Plant if kind == "plants" else Region
    assert current_model.objects.filter(pk=duplicate_id, deleted_at=NOW).exists()
    assert current_model.objects.filter(pk=original_id, deleted_at__isnull=True).exists()
    assert PlantConfiguration.objects.values().get(pk=configuration_id) == (previous_configuration)
