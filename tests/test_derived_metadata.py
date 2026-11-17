from datetime import timedelta

import pytest
from django.core.exceptions import FieldDoesNotExist

from pyracast.api.serializers import BindingInput, RegionOutput
from pyracast.calculation import calculate
from pyracast.connections.contracts import ProviderData
from pyracast.connections.models import SiteBinding
from pyracast.regions.models import Region
from tests.support.domain import NOW, make_system

pytestmark = pytest.mark.django_db(transaction=True)


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
