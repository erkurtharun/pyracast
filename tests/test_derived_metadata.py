from datetime import timedelta

import pytest
from django.core.exceptions import FieldDoesNotExist

from pyracast.api.serializers import RegionOutput
from pyracast.calculation import calculate
from pyracast.connections.contracts import ProviderData
from pyracast.connections.models import SiteBinding
from pyracast.intervals import NormalizedInterval
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
            NormalizedInterval.from_rows(
                [
                    {
                        "start": NOW.isoformat(),
                        "end": (NOW + timedelta(minutes=30)).isoformat(),
                        "power_kw": 0.5,
                    }
                ]
            ),
            collected_at=NOW,
        ),
    )
    assert payload["surface"] == "horizontal"
    assert isinstance(payload["configuration"], dict)
    assert payload["configuration"]["capacity_basis"] == "dc"
    assert payload["configuration"]["power_basis"] == "dc"
