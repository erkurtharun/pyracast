from datetime import date

from django.urls import reverse

from pyracast.connections.secrets import encrypt_secret
from pyracast.plants.models import Plant, PlantSubmission
from tests.support.domain import System, make_system


def admin_system() -> System:
    system = make_system()
    system.account.provider = "solcast"
    system.account.encrypted_api_key = encrypt_secret("original-fixture-key")
    system.account.save(update_fields=("provider", "encrypted_api_key"))
    system.binding.installation_date = date(2020, 1, 1)
    system.binding.settings_verification_reference = "Verified synthetic site settings"
    system.binding.save(update_fields=("installation_date", "settings_verification_reference"))
    return system


def setup_url(system: System) -> str:
    return reverse("admin:regions_region_setup", args=(system.region.pk,))


def setup_input(system: System, **changes: object) -> dict[str, object]:
    return {
        "action": "configure",
        "expected_revision": system.region.revision,
        "expected_binding_id": system.binding.pk,
        "account_mode": "existing",
        "existing_account": f"{system.account.pk}:{system.account.credential_revision}",
        "api_key": "",
        "daily_quota": 10,
        "site_id": "solar-resource",
        "capacity_kw": 1000,
        "dc_capacity_kw": "",
        "power_basis": "ac",
        "installation_date": "2020-01-01",
        "azimuth_degrees": 0,
        "losses_percent": 0,
        "settings_verification_reference": "Checked site settings with authorized account owner",
        "capacity_semantics_verified": True,
        **changes,
    }


def submitted_plant(system: System) -> Plant:
    plant = Plant.objects.create(region=system.region, name="Customer solar park")
    PlantSubmission.objects.create(
        plant=plant,
        dc_capacity_mw=4.2,
        submitted_by=system.member,
        inverter_capacity_mw=4,
        export_limit_mw=3.8,
    )
    return plant
