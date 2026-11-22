from typing import cast

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.test import Client

from pyracast.accounts.models import Membership, Tenant
from pyracast.accounts.operations import remove_workspace_member
from pyracast.epias import EpiasClient
from pyracast.metering import operations
from pyracast.metering.jobs import run_epias_job
from pyracast.metering.models import EpiasCollectionJob, EpiasConnection
from pyracast.notifications import recipients
from pyracast.plants.models import Plant, PlantEvent
from tests.support.api import verified_admin
from tests.support.concurrency import paused_in
from tests.support.metering import (
    PROVIDER_PLANT,
    FixtureClient,
    connect,
    connected,
    day_data,
)
from tests.support.plant_api import setup_plant
from tests.support.plant_operations import make_plant

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


@pytest.mark.parametrize("change", ["connect", "disconnect"])
def test_a_connection_change_waits_for_its_administrators_account_before_the_workspace(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    # The removal holds the person's account and then locks the workspace's regions; a connection
    # change by that person that held the region while waiting for the account would close a cycle.
    system, plant, _linked = connected(monkeypatch)
    Membership.objects.create(user=system.admin, tenant=system.tenant)
    if change == "connect":
        operations.disconnect_epias(plant.pk, actor=system.admin)

    def change_connection() -> object:
        if change == "connect":
            return connect(plant, system.admin).enabled
        return operations.disconnect_epias(plant.pk, actor=system.admin)

    removed, changed = paused_in(
        monkeypatch,
        recipients,
        "remove_member_recipients",
        lambda: remove_workspace_member(system.member, system.tenant.pk, system.admin.pk),
        change_connection,
    )

    assert (removed, changed) == (True, True)
    assert not Membership.objects.filter(user=system.admin).exists()
    assert EpiasConnection.objects.get(plant=plant).enabled is (change == "connect")


def test_admin_without_membership_connects_and_tenants_keep_isolated_status_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member, base, data = setup_plant()
    plant = Plant.objects.get(pk=data["id"])
    source = f"{base}{plant.pk}/production-source/"
    admin, actor = verified_admin()
    assert not Membership.objects.filter(user=actor).exists()
    monkeypatch.setattr(operations, "get_epias_client", lambda: FixtureClient())
    operations.connect_epias(
        plant.pk,
        actor=actor,
        provider_plant_id=PROVIDER_PLANT.id,
        expected_eic=cast(str, PROVIDER_PLANT.eic),
    )
    linked = EpiasConnection.objects.get(plant=plant)
    assert linked.connected_by_id == actor.pk
    assert PlantEvent.objects.get(plant=plant, action="epias_connected").actor_id == actor.pk
    job = EpiasCollectionJob.objects.filter(connection=linked).latest("target_date")
    assert run_epias_job(job.pk, fetch=day_data)
    assert member.get(source).json()["archived_days"] == 1
    outsider = User.objects.create_user("other-member")
    Membership.objects.create(user=outsider, tenant=Tenant.objects.create(name="Other"))
    foreign = Client()
    foreign.force_login(outsider)
    for viewer in (foreign, admin):
        assert viewer.get(source).status_code == 403
    operations.disconnect_epias(plant.pk, actor=actor)
    assert PlantEvent.objects.get(plant=plant, action="epias_disconnected").actor_id == actor.pk
    assert member.get(source).json()["connected"] is False
    assert member.get(source).json()["archived_days"] == 1
