from typing import cast

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.test import Client

from pyracast.accounts.models import Membership, Tenant
from pyracast.epias import EpiasClient
from pyracast.metering import operations
from pyracast.metering.jobs import run_epias_job
from pyracast.metering.models import EpiasCollectionJob, EpiasConnection
from pyracast.plants.models import Plant, PlantEvent
from tests.test_api import verified_admin
from tests.test_metering import PROVIDER_PLANT, FixtureClient, connect, day_data
from tests.test_plant_api import setup_plant
from tests.test_plant_operations import make_plant

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("identity", ["member", "staff", "inactive_admin"])
def test_connection_operations_require_admin_before_lookup_or_provider_access(
    monkeypatch: pytest.MonkeyPatch, identity: str
) -> None:
    system, plant = make_plant(monkeypatch)
    linked = connect(plant, system.admin)
    actor = system.member
    if identity != "member":
        actor = User.objects.create(
            username=identity,
            is_staff=True,
            is_superuser=identity == "inactive_admin",
            is_active=identity != "inactive_admin",
        )
    provider = FixtureClient()
    for plant_id in (plant.pk, -1):
        with pytest.raises(PermissionDenied):
            operations.connect_epias(
                plant_id,
                actor=actor,
                provider_plant_id=PROVIDER_PLANT.id,
                expected_eic=cast(str, PROVIDER_PLANT.eic),
                client=cast(EpiasClient, provider),
            )
        with pytest.raises(PermissionDenied):
            operations.disconnect_epias(plant_id, actor=actor)
    linked.refresh_from_db()
    assert linked.enabled
    assert linked.revision == 1
    assert provider.calls == 0
    assert PlantEvent.objects.filter(plant=plant, action="epias_connected").count() == 1
    assert not PlantEvent.objects.filter(plant=plant, action="epias_disconnected").exists()


@pytest.mark.parametrize("identity", ["member", "staff", "unverified_admin", "inactive_admin"])
def test_api_connection_mutations_require_verified_admin_before_validation(
    monkeypatch: pytest.MonkeyPatch, identity: str
) -> None:
    member, base, plant = setup_plant()
    source = f"{base}{plant['id']}/production-source/"
    viewer = member
    if identity == "inactive_admin":
        viewer, actor = verified_admin()
        actor.is_active = False
        actor.save(update_fields=("is_active",))
    elif identity != "member":
        actor = User.objects.create_user(
            username=identity, is_staff=True, is_superuser=identity == "unverified_admin"
        )
        viewer = Client()
        viewer.force_login(actor)

    def forbidden_provider() -> EpiasClient:
        pytest.fail("Unauthorized mutations must not access provider credentials.")

    monkeypatch.setattr(operations, "get_epias_client", forbidden_provider)
    assert viewer.put(source, {}, content_type="application/json").status_code == 403
    assert viewer.put(source, "{", content_type="application/json").status_code == 403
    assert (
        viewer.put(
            source,
            {"provider_plant_id": PROVIDER_PLANT.id, "expected_eic": PROVIDER_PLANT.eic},
            content_type="application/json",
        ).status_code
        == 403
    )
    assert viewer.delete(source).status_code == 403
    assert not EpiasConnection.objects.exists()
    assert not PlantEvent.objects.filter(action__startswith="epias_").exists()


def test_admin_without_membership_mutates_and_tenants_keep_isolated_archive_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member, base, data = setup_plant()
    plant = Plant.objects.get(pk=data["id"])
    source = f"{base}{plant.pk}/production-source/"
    history = f"{base}{plant.pk}/reported-production/"
    admin, actor = verified_admin()
    assert not Membership.objects.filter(user=actor).exists()
    monkeypatch.setattr(operations, "get_epias_client", lambda: FixtureClient())
    response = admin.put(
        source,
        {"provider_plant_id": PROVIDER_PLANT.id, "expected_eic": PROVIDER_PLANT.eic},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    assert response.json()["connected"] is True
    linked = EpiasConnection.objects.get(plant=plant)
    assert linked.connected_by_id == actor.pk
    assert PlantEvent.objects.get(plant=plant, action="epias_connected").actor_id == actor.pk
    job = EpiasCollectionJob.objects.filter(connection=linked).latest("target_day")
    assert run_epias_job(job.pk, fetch=day_data)
    assert member.get(source).json()["archived_days"] == 1
    archive = member.get(history, {"date": str(job.target_day)})
    assert archive.status_code == 200
    assert archive.json()["snapshot"]["id"] is not None

    outsider = User.objects.create_user("other-member")
    Membership.objects.create(user=outsider, tenant=Tenant.objects.create(name="Other"))
    foreign = Client()
    foreign.force_login(outsider)
    for viewer in (foreign, admin):
        assert viewer.get(source).status_code == 403
        assert viewer.get(history).status_code == 403
    assert admin.delete(source).status_code == 204
    assert PlantEvent.objects.get(plant=plant, action="epias_disconnected").actor_id == actor.pk
    assert member.get(source).json()["connected"] is False
    disconnected = member.get(history, {"date": str(job.target_day)}).json()
    assert disconnected["snapshot"] == archive.json()["snapshot"]
    assert disconnected["readings"] == archive.json()["readings"]
    assert disconnected["calibration_blocker"] == "source_disconnected"


def test_member_cannot_disconnect_existing_epias_connection() -> None:
    member, base, plant = setup_plant()
    linked = EpiasConnection.objects.create(
        plant_id=plant["id"],
        provider_plant_id=PROVIDER_PLANT.id,
        eic=cast(str, PROVIDER_PLANT.eic),
        name=PROVIDER_PLANT.name,
        connected_by_id=member.session["_auth_user_id"],
    )
    assert member.delete(f"{base}{plant['id']}/production-source/").status_code == 403
    linked.refresh_from_db()
    assert linked.enabled
    assert not PlantEvent.objects.filter(action="epias_disconnected").exists()


@pytest.mark.parametrize("scope", ["tenant", "region", "plant"])
def test_admin_mutations_validate_url_scope_before_provider_access(
    monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    _, _, data = setup_plant()
    plant = Plant.objects.select_related("region").get(pk=data["id"])
    tenant_id = plant.region.tenant_id + 1000000 if scope == "tenant" else plant.region.tenant_id
    region_id = plant.region_id + 1000000 if scope == "region" else plant.region_id
    plant_id = plant.pk + 1000000 if scope == "plant" else plant.pk
    source = f"/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/production-source/"
    admin, _ = verified_admin()

    def forbidden_provider() -> EpiasClient:
        pytest.fail("Invalid scopes must not access provider credentials.")

    monkeypatch.setattr(operations, "get_epias_client", forbidden_provider)
    assert admin.put(source, {}, content_type="application/json").status_code == 404
    assert admin.delete(source).status_code == 404
