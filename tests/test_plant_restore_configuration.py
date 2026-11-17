import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from pyracast.connections.operations import validate_binding
from pyracast.plants.models import Plant, PlantConfiguration
from pyracast.plants.operations import create_plant
from pyracast.regions.operations import approve_region, configure_binding, submit_region
from pyracast.trash import restore_plant, trash_plant
from tests.support.domain import NOW, FakeAdapter, make_system

pytestmark = pytest.mark.django_db(transaction=True)


def test_restore_activates_submission_approved_while_trashed() -> None:
    system = make_system()
    plant = create_plant(
        system.member, system.tenant.pk, system.region.pk, name="Plant", dc_capacity_mw=120
    )
    args = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    trash_plant(*args)
    validate_binding(system.admin, system.binding.pk, FakeAdapter(), NOW)
    approve_region(system.admin, system.region.pk, system.binding.pk, NOW)
    restored = restore_plant(*args)
    assert restored.current_configuration is not None
    assert restored.current_configuration.binding_id == system.binding.pk
    assert restored.current_configuration.dc_capacity_mw == 120


def test_restore_rebinds_historical_configuration() -> None:
    system = make_system(approved=True)
    plant = create_plant(
        system.member, system.tenant.pk, system.region.pk, name="Plant", dc_capacity_mw=120
    )
    old = plant.current_configuration
    assert old is not None
    args = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    trash_plant(*args)
    replacement = configure_binding(
        system.admin,
        system.region.pk,
        provider_account_id=system.account.pk,
        site_id="replacement",
        capacity_kw=2,
        capacity_semantics_verified=True,
    )
    validate_binding(system.admin, replacement.pk, FakeAdapter(), NOW)
    approve_region(system.admin, system.region.pk, replacement.pk, NOW)
    restored = restore_plant(*args)
    assert restored.current_configuration is not None
    assert restored.current_configuration.binding_id == replacement.pk
    assert restored.current_configuration.dc_capacity_mw == old.dc_capacity_mw
    old.refresh_from_db()
    assert old.binding_id == system.binding.pk
    assert PlantConfiguration.objects.filter(plant=plant).count() == 2


def test_restore_unchanged_binding_and_pending_do_not_create_configuration() -> None:
    system = make_system(approved=True)
    plant = create_plant(
        system.member, system.tenant.pk, system.region.pk, name="Plant", dc_capacity_mw=120
    )
    original = plant.current_configuration_id
    args = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    trash_plant(*args)
    assert restore_plant(*args).current_configuration_id == original
    trash_plant(*args)
    system.region.status = "pending"
    system.region.save(update_fields=("status",))
    assert restore_plant(*args).current_configuration_id == original
    assert PlantConfiguration.objects.filter(plant=plant).count() == 1


def test_restore_legacy_empty_plant() -> None:
    system = make_system(approved=True)
    plant = Plant.objects.create(region=system.region, name="Legacy", deleted_at=timezone.now())
    assert (
        restore_plant(
            system.member, system.tenant.pk, system.region.pk, plant.pk
        ).current_configuration_id
        is None
    )


def test_restore_does_not_use_another_regions_binding() -> None:
    system = make_system()
    plant = create_plant(
        system.member, system.tenant.pk, system.region.pk, name="Plant", dc_capacity_mw=120
    )
    args = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    trash_plant(*args)
    other = submit_region(
        system.member, system.tenant.pk, name="Other", latitude=41, longitude=29, timezone="UTC"
    )
    binding = configure_binding(
        system.admin,
        other.pk,
        provider_account_id=system.account.pk,
        site_id="other",
        capacity_kw=1,
        capacity_semantics_verified=True,
    )
    validate_binding(system.admin, binding.pk, FakeAdapter(), NOW)
    system.region.status = "approved"
    system.region.approved_binding = binding

    def restore_with_invalid_approval() -> None:
        with transaction.atomic():
            system.region.save(update_fields=("status", "approved_binding"))
            assert restore_plant(*args).current_configuration_id is None
            assert not PlantConfiguration.objects.filter(plant=plant).exists()

    with pytest.raises(IntegrityError, match="approved_binding_region_fk"):
        restore_with_invalid_approval()
    plant.refresh_from_db()
    assert plant.deleted_at is not None
    assert not PlantConfiguration.objects.filter(plant=plant).exists()


@pytest.mark.parametrize("state", ["pending", "suspended", "disabled", "credential", "unvalidated"])
def test_restore_does_not_activate_without_current_approval(state: str) -> None:
    system = make_system()
    plant = create_plant(
        system.member, system.tenant.pk, system.region.pk, name="Plant", dc_capacity_mw=120
    )
    args = (system.member, system.tenant.pk, system.region.pk, plant.pk)
    trash_plant(*args)
    validate_binding(system.admin, system.binding.pk, FakeAdapter(), NOW)
    approve_region(system.admin, system.region.pk, system.binding.pk, NOW)
    if state in {"pending", "suspended"}:
        system.region.status = state
        system.region.save(update_fields=("status",))
    elif state == "disabled":
        system.account.enabled = False
        system.account.save(update_fields=("enabled",))
    elif state == "credential":
        system.account.credential_revision += 1
        system.account.save(update_fields=("credential_revision",))
    else:
        system.binding.validated_at = None
        system.binding.save(update_fields=("validated_at",))
    assert restore_plant(*args).current_configuration_id is None
    assert not PlantConfiguration.objects.filter(plant=plant).exists()
