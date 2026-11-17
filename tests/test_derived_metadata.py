from datetime import timedelta

import pytest
from django.core.exceptions import FieldDoesNotExist
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor

from pyracast.api.serializers import BindingInput, RegionOutput
from pyracast.calculation import calculate
from pyracast.connections.contracts import ProviderData
from pyracast.connections.models import SiteBinding
from pyracast.regions.models import Region
from tests.support.domain import NOW, make_system

pytestmark = pytest.mark.django_db(transaction=True)

REGION_MIGRATION = "0008_remove_region_surface"
BINDING_MIGRATION = "0010_remove_sitebinding_binding_matching_power_basis_and_more"


def test_obsolete_reference_metadata_is_absent_from_models_and_region_api() -> None:
    system = make_system()
    assert "surface" not in RegionOutput(system.region).data
    assert not hasattr(system.region, "surface")
    assert not hasattr(system.binding, "capacity_basis")
    for model, field in ((Region, "surface"), (SiteBinding, "capacity_basis")):
        with pytest.raises(FieldDoesNotExist):
            model._meta.get_field(field)
    SiteBinding.objects.filter(pk=system.binding.pk).update(power_basis="dc")
    system.binding.refresh_from_db()
    payload = calculate(
        system.binding,
        NOW.date(),
        ProviderData(
            {},
            [
                {
                    "start": NOW.isoformat(),
                    "end": (NOW + timedelta(minutes=30)).isoformat(),
                    "power_kw": 0.5,
                }
            ],
            collected_at=NOW,
        ),
    )
    assert payload["surface"] == "horizontal"
    assert isinstance(payload["configuration"], dict)
    assert payload["configuration"]["capacity_basis"] == "dc"
    assert payload["configuration"]["power_basis"] == "dc"


def test_binding_input_rejects_obsolete_capacity_basis_field() -> None:
    system = make_system()
    serializer = BindingInput(
        data={
            "provider_account_id": system.account.pk,
            "site_id": "site",
            "capacity_kw": 1,
            "capacity_semantics_verified": True,
            "installation_date": "2020-01-01",
            "power_basis": "ac",
            "capacity_basis": "dc",
        }
    )
    assert not serializer.is_valid()
    assert "Unknown fields" in str(serializer.errors)
    assert SiteBinding.objects.count() == 1


def test_surface_removal_refuses_legacy_values_and_preserves_them_on_failure() -> None:
    system = make_system()
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        executor = MigrationExecutor(connection)
        executor.migrate([("regions", "0007_validate_approved_binding_region")])
        legacy_region = executor.loader.project_state(
            [("regions", "0007_validate_approved_binding_region")]
        ).apps.get_model("regions", "Region")
        legacy_region.objects.filter(pk=system.region.pk).update(surface="tilted")
        with pytest.raises(RuntimeError, match="non-horizontal regions exist"):
            MigrationExecutor(connection).migrate([("regions", REGION_MIGRATION)])
        assert legacy_region.objects.get(pk=system.region.pk).surface == "tilted"
        legacy_region.objects.filter(pk=system.region.pk).update(surface="horizontal")
        MigrationExecutor(connection).migrate(
            [("regions", REGION_MIGRATION), ("connections", BINDING_MIGRATION)]
        )
        transaction.set_rollback(True)


def test_basis_removal_restores_dc_on_reverse_and_refuses_legacy_mismatch() -> None:
    system = make_system()
    SiteBinding.objects.filter(pk=system.binding.pk).update(power_basis="dc")
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        executor = MigrationExecutor(connection)
        target = [("connections", "0009_validate_binding_integrity")]
        executor.migrate(target)
        legacy_binding = executor.loader.project_state(target).apps.get_model(
            "connections", "SiteBinding"
        )
        restored = legacy_binding.objects.get(pk=system.binding.pk)
        assert restored.capacity_basis == restored.power_basis == "dc"
        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE connections_sitebinding DROP CONSTRAINT binding_matching_power_basis"
            )
        legacy_binding.objects.filter(pk=restored.pk).update(capacity_basis="ac")
        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE connections_sitebinding ADD CONSTRAINT binding_matching_power_basis "
                "CHECK (power_basis IN ('ac', 'dc') AND capacity_basis = power_basis) NOT VALID"
            )
        with pytest.raises(RuntimeError, match="mismatched power and capacity bases exist"):
            MigrationExecutor(connection).migrate([("connections", BINDING_MIGRATION)])
        assert legacy_binding.objects.get(pk=restored.pk).capacity_basis == "ac"
        transaction.set_rollback(True)
