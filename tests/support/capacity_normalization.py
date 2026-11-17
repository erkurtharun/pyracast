import pytest

from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.connections.secrets import encrypt_secret
from pyracast.forecasts.capacity_normalization import normalize_reference_capacity
from tests.support.domain import System
from tests.support.plant_operations import make_plant


def scaled_system(monkeypatch: pytest.MonkeyPatch) -> tuple[System, SiteBinding]:
    system, _ = make_plant(monkeypatch)
    ProviderAccount.objects.filter(pk=system.account.pk).update(
        provider="solcast", encrypted_api_key=encrypt_secret("test-only-solcast-key")
    )
    SiteBinding.objects.filter(pk=system.binding.pk).update(
        settings_verification_reference="Verified original Hobby settings", dc_capacity_kw=1
    )
    target = SiteBinding.objects.get(pk=system.binding.pk)
    target.pk = None
    target.capacity_kw = target.dc_capacity_kw = 1000
    target.save()
    system.region.approved_binding = target
    system.region.save(update_fields=["approved_binding"])
    return system, target


def normalize(system: System, target: SiteBinding) -> tuple[int, ...]:
    return normalize_reference_capacity(
        system.admin,
        system.tenant.pk,
        system.region.pk,
        system.binding.pk,
        target.pk,
        "User confirms the same Hobby model with only a 1 to 1000 kW reference scale change",
    )
