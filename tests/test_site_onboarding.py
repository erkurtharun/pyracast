from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.test import Client
from django.utils import timezone

from pyracast.accounts.models import Membership, Tenant
from pyracast.connections.operations import validate_binding
from pyracast.forecasts.models import CollectionJob
from pyracast.plants.models import Plant, PlantConfiguration, PlantSubmission
from pyracast.plants.operations import create_plant, revise_plant
from pyracast.plants.sites import site_queryset, submit_site
from pyracast.regions.models import Region
from pyracast.regions.operations import (
    approve_region,
    configure_binding,
    request_location_change,
    review_location_change,
    submit_region,
)
from pyracast.trash import purge_expired, restore_plant, restore_region, trash_plant, trash_region
from tests.test_domain import NOW, FakeAdapter, System, make_system

pytestmark = pytest.mark.django_db(transaction=True)


def site_values(**updates: Any) -> dict[str, Any]:
    return {
        "name": "New plant",
        "latitude": 41.1234561,
        "longitude": 30.1234561,
        "timezone": "Europe/Istanbul",
        "dc_capacity_mw": 120,
        "inverter_capacity_mw": None,
        "export_limit_mw": 100,
        "measurement_basis": "net_export",
        "mounting_type": "fixed",
        "surface_tilt": 25,
        "surface_azimuth": 180,
        **updates,
    }


def pending_plant(system: System) -> Plant:
    return create_plant(
        system.member, system.tenant.pk, system.region.pk, name="Pending", dc_capacity_mw=120
    )


def test_combined_site_api_saves_technical_submission_before_provider_approval() -> None:
    system = make_system()
    client = Client()
    client.force_login(system.member)
    base = f"/api/tenants/{system.tenant.pk}/sites/"
    response = client.post(base, site_values(), content_type="application/json")
    assert response.status_code == 201, response.content
    site = response.json()
    assert site["status"] == "pending"
    assert site["latitude"] == 41.123456
    assert site["plant"]["current_configuration"] is None
    assert site["plant"]["submission"]["dc_capacity_mw"] == 120
    assert site["plant"]["submission"]["inverter_capacity_mw"] is None
    assert site["plant"]["submission"]["export_limit_mw"] == 100
    assert PlantConfiguration.objects.count() == CollectionJob.objects.count() == 0
    assert client.get(f"{base}{site['id']}/").json() == site
    rows = client.get(base).json()["results"]
    assert rows[0]["plant"] is None
    assert rows[1] == site
    plant_url = (
        f"/api/tenants/{system.tenant.pk}/regions/{site['id']}/plants/{site['plant']['id']}/"
    )
    updated = client.put(
        plant_url, {"name": "Updated", "dc_capacity_mw": 130}, content_type="application/json"
    )
    assert updated.status_code == 200
    assert updated.json()["current_configuration"] is None
    assert updated.json()["submission"]["dc_capacity_mw"] == 130
    assert PlantSubmission.objects.count() == 1


def test_site_api_rejects_invalid_values_and_cross_tenant_access_atomically() -> None:
    system = make_system()
    client = Client()
    client.force_login(system.member)
    base = f"/api/tenants/{system.tenant.pk}/sites/"
    for updates in ({"dc_capacity_mw": 0}, {"surface_tilt": 91}, {"secret": "unaccepted"}):
        response = client.post(base, site_values(**updates), content_type="application/json")
        assert response.status_code == 400
    assert Region.objects.count() == 1
    assert not Plant.objects.exists()
    other = Tenant.objects.create(name="Other")
    for suffix in ("", f"{system.region.pk}/"):
        assert client.get(f"/api/tenants/{other.pk}/sites/{suffix}").status_code == 403
    assert (
        client.post(
            f"/api/tenants/{other.pk}/sites/", site_values(), content_type="application/json"
        ).status_code
        == 403
    )
    assert Region.objects.count() == 1


def test_site_list_prepares_plants_without_per_row_queries(django_assert_num_queries: Any) -> None:
    system = make_system()
    for index in range(4):
        submit_site(system.member, system.tenant.pk, **site_values(latitude=41 + index))
    from pyracast.plants.site_api import SiteOutput

    with django_assert_num_queries(3):
        rows = SiteOutput(site_queryset(system.member, system.tenant.pk), many=True).data
    assert len(rows) == 5


def test_approval_materializes_submission_once_and_preserves_customer_declaration() -> None:
    system = make_system()
    plant = pending_plant(system)
    submission = PlantSubmission.objects.get(plant=plant)
    revise_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        name="Declared",
        dc_capacity_mw=140,
        mounting_type="tracking",
    )
    validate_binding(system.admin, system.binding.pk, FakeAdapter(), NOW)
    job = approve_region(system.admin, system.region.pk, system.binding.pk, NOW)
    assert approve_region(system.admin, system.region.pk, system.binding.pk, NOW).pk == job.pk
    plant.refresh_from_db()
    assert plant.current_configuration is not None
    assert plant.current_configuration.dc_capacity_mw == 140
    assert plant.current_configuration.mounting_type == "tracking"
    assert plant.current_configuration.binding_id == system.binding.pk
    assert PlantConfiguration.objects.count() == 1
    submission.refresh_from_db()
    assert submission.dc_capacity_mw == 140
    revise_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        name="Later",
        dc_capacity_mw=150,
    )
    submission.refresh_from_db()
    assert submission.dc_capacity_mw == 140
    assert PlantConfiguration.objects.count() == 2


def test_invalid_approval_keeps_submission_and_creates_no_configuration() -> None:
    system = make_system()
    plant = pending_plant(system)
    with pytest.raises(ValidationError, match="validated"):
        approve_region(system.admin, system.region.pk, system.binding.pk, NOW)
    plant.refresh_from_db()
    assert plant.current_configuration_id is None
    assert not PlantConfiguration.objects.exists()
    assert not CollectionJob.objects.exists()


def test_reapproval_rebinds_current_technical_parameters_without_rewriting_history() -> None:
    system = make_system()
    plant = pending_plant(system)
    validate_binding(system.admin, system.binding.pk, FakeAdapter(), NOW)
    approve_region(system.admin, system.region.pk, system.binding.pk, NOW)
    plant = revise_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        name="Revised",
        dc_capacity_mw=150,
        export_limit_mw=100,
    )
    previous = plant.current_configuration
    assert previous is not None
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
    plant.refresh_from_db()
    previous.refresh_from_db()
    assert plant.current_configuration is not None
    assert plant.current_configuration.revision == 3
    assert plant.current_configuration.binding_id == replacement.pk
    assert plant.current_configuration.dc_capacity_mw == 150
    assert previous.binding_id == system.binding.pk
    assert PlantSubmission.objects.get(plant=plant).dc_capacity_mw == 120
    system.region.refresh_from_db()
    system.region.status = "suspended"
    system.region.save(update_fields=("status",))
    approve_region(system.admin, system.region.pk, replacement.pk, NOW)
    assert PlantConfiguration.objects.count() == 3


def test_approval_rejects_missing_technical_declaration_atomically() -> None:
    system = make_system()
    Plant.objects.create(region=system.region, name="Incomplete legacy plant")
    validate_binding(system.admin, system.binding.pk, FakeAdapter(), NOW)
    with pytest.raises(ValidationError, match="plant_submission_required"):
        approve_region(system.admin, system.region.pk, system.binding.pk, NOW)
    system.region.refresh_from_db()
    assert system.region.status == "pending"
    assert system.region.approved_binding_id is None
    assert not PlantConfiguration.objects.exists()
    assert not CollectionJob.objects.exists()


def test_duplicate_locations_use_six_decimal_tenant_scoped_identity() -> None:
    system = make_system()
    site = submit_site(system.member, system.tenant.pk, **site_values())
    with pytest.raises(ValidationError, match="location_already_exists"):
        submit_site(system.member, system.tenant.pk, **site_values(latitude=41.1234562))
    other = Tenant.objects.create(name="Other")
    Membership.objects.create(user=system.member, tenant=other, role=Membership.Role.ADMIN)
    assert submit_site(system.member, other.pk, **site_values()).pk != site.pk
    with pytest.raises(IntegrityError), transaction.atomic():
        Region.objects.create(
            tenant=system.tenant,
            name="Direct",
            latitude=41.1234563,
            longitude=30.1234563,
            timezone="UTC",
        )


def test_plant_and_location_restore_reject_replacement_conflicts() -> None:
    system = make_system()
    plant = pending_plant(system)
    parent = (system.member, system.tenant.pk, system.region.pk)
    with pytest.raises(ValidationError, match="location_already_has_plant"):
        pending_plant(system)
    with pytest.raises(IntegrityError), transaction.atomic():
        Plant.objects.create(region=system.region, name="Direct")
    trash_plant(*parent, plant.pk)
    replacement = pending_plant(system)
    with pytest.raises(ValidationError, match="location_already_has_plant"):
        restore_plant(*parent, plant.pk)
    trash_plant(*parent, replacement.pk)
    assert restore_plant(*parent, plant.pk).deleted_at is None
    trash_region(*parent)
    replacement_region = submit_region(
        system.member,
        system.tenant.pk,
        name="Replacement",
        latitude=40,
        longitude=29,
        timezone="Europe/Istanbul",
    )
    with pytest.raises(ValidationError, match="location_already_exists"):
        restore_region(*parent)
    trash_region(system.member, system.tenant.pk, replacement_region.pk)
    assert restore_region(*parent).deleted_at is None
    assert PlantSubmission.objects.filter(plant=plant).exists()


def test_location_review_rejects_duplicate_without_changing_approval() -> None:
    system = make_system(approved=True)
    submit_site(system.member, system.tenant.pk, **site_values())
    change = request_location_change(
        system.member,
        system.region.pk,
        system.tenant.pk,
        expected_revision=1,
        latitude=41.1234562,
        longitude=30.1234562,
        timezone="UTC",
    )
    with pytest.raises(ValidationError, match="location_already_exists"):
        review_location_change(system.admin, change.pk, approve=True)
    system.region.refresh_from_db()
    change.refresh_from_db()
    assert system.region.status == "approved"
    assert change.status == "pending"


@pytest.mark.parametrize("kind", ["site", "plant"])
def test_concurrent_site_and_plant_submission_preserve_uniqueness(kind: str) -> None:
    system = make_system()
    barrier = Barrier(2)

    def submit() -> str:
        close_old_connections()
        try:
            actor = User.objects.get(pk=system.member.pk)
            barrier.wait(timeout=10)
            if kind == "site":
                submit_site(actor, system.tenant.pk, **site_values())
            else:
                create_plant(actor, system.tenant.pk, system.region.pk, name="PV", dc_capacity_mw=1)
            return "created"
        except ValidationError:
            return "duplicate"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: submit(), range(2)))
    assert sorted(outcomes) == ["created", "duplicate"]
    assert Plant.objects.count() == PlantSubmission.objects.count() == 1


def test_pending_submission_is_purged_with_its_plant() -> None:
    system = make_system()
    plant = pending_plant(system)
    trash_plant(system.member, system.tenant.pk, system.region.pk, plant.pk)
    from pyracast.trash import RETENTION

    assert purge_expired(timezone.now() + RETENTION) == {"regions": 0, "plants": 1}
    assert not PlantSubmission.objects.exists()
