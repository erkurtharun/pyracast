from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import close_old_connections, connections, transaction
from django.test import Client
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from pyracast import trash
from pyracast.accounts.models import Membership, Tenant
from pyracast.accounts.operations import reassign_workspace_admin
from pyracast.api.permissions import WorkspaceWritePermission
from pyracast.forecasts.models import CollectionJob, CollectionSchedule
from pyracast.forecasts.schedule import configure_schedule
from pyracast.incidents.operations import finish_incident, save_incident
from pyracast.notifications.models import ReminderSchedule
from pyracast.notifications.operations import configure_reminders
from pyracast.plants import operations
from pyracast.plants.automation import set_calibration_automation
from pyracast.plants.models import Plant, PlantEvent
from pyracast.plants.sites import submit_site
from pyracast.regions.models import AuditEvent, Region, RegionLocationChangeRequest
from pyracast.regions.operations import request_location_change, submit_region
from tests.support.domain import NOW, System, make_system

pytestmark = pytest.mark.django_db(transaction=True)


def test_workspace_write_permission_rejects_anonymous_callers() -> None:
    request = Request(APIRequestFactory().post("/api/tenants/1/regions/"))
    view = APIView()
    view.kwargs = {"tenant_id": 1}
    assert not WorkspaceWritePermission().has_permission(request, view)


@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("post", "sites/"),
        ("delete", "regions/{region}/"),
        ("post", "regions/{region}/location-change-requests/"),
        ("post", "regions/{region}/restore/"),
        ("put", "regions/{region}/collection-schedule/"),
        ("put", "regions/{region}/reminders/"),
        ("post", "regions/{region}/plants/"),
        ("put", "regions/{region}/plants/{plant}/"),
        ("delete", "regions/{region}/plants/{plant}/"),
        ("post", "regions/{region}/plants/{plant}/restore/"),
        ("post", "regions/{region}/plants/{plant}/calibrations/"),
        ("post", "regions/{region}/plants/{plant}/calibrations/1/activate/"),
        ("post", "regions/{region}/plants/{plant}/calibration/reset/"),
        ("patch", "regions/{region}/plants/{plant}/calibration/automation/"),
        ("post", "regions/{region}/plants/{plant}/outages/"),
        ("put", "regions/{region}/plants/{plant}/outages/1/"),
        ("post", "regions/{region}/plants/{plant}/outages/1/resolve/"),
        ("post", "regions/{region}/plants/{plant}/outages/1/cancel/"),
        ("post", "invitations/"),
        ("delete", "invitations/1/"),
    ],
)
def test_viewer_cannot_mutate_any_workspace_endpoint(method: str, suffix: str) -> None:
    system = make_system(approved=True)
    plant = operations.create_plant(
        system.member, system.tenant.pk, system.region.pk, name="PV", dc_capacity_mw=1
    )
    viewer = User.objects.create_user(username="viewer")
    Membership.objects.create(user=viewer, tenant=system.tenant)
    client = Client()
    client.force_login(viewer)
    before = (Region.objects.count(), Plant.objects.count(), AuditEvent.objects.count())
    path = f"/api/tenants/{system.tenant.pk}/" + suffix.format(
        region=system.region.pk, plant=plant.pk
    )
    response = getattr(client, method)(path, {}, content_type="application/json")
    assert response.status_code == 403, response.content
    assert (Region.objects.count(), Plant.objects.count(), AuditEvent.objects.count()) == before
    system.region.refresh_from_db()
    plant.refresh_from_db()
    assert system.region.deleted_at is None
    assert plant.deleted_at is None
    assert not CollectionSchedule.objects.exists()
    assert not ReminderSchedule.objects.exists()


def mutation_calls(system: System, actor: User, plant: Plant) -> list[Callable[[], object]]:
    tenant_id, region_id, plant_id = system.tenant.pk, system.region.pk, plant.pk
    return [
        lambda: submit_region(
            actor, tenant_id, name="New", latitude=41, longitude=30, timezone="UTC"
        ),
        lambda: request_location_change(
            actor,
            region_id,
            tenant_id,
            expected_revision=1,
            latitude=41,
            longitude=30,
            timezone="UTC",
        ),
        lambda: submit_site(
            actor,
            tenant_id,
            name="New",
            latitude=41,
            longitude=30,
            timezone="UTC",
            dc_capacity_mw=1,
        ),
        lambda: operations.create_plant(actor, tenant_id, region_id, name="New", dc_capacity_mw=1),
        lambda: operations.revise_plant(
            actor, tenant_id, region_id, plant_id, name="Changed", dc_capacity_mw=2
        ),
        lambda: operations.import_observations(
            actor,
            tenant_id,
            region_id,
            plant_id,
            configuration_id=1,
            unit="MWh",
            observations=[operations.ObservationInput(NOW - timedelta(hours=1), NOW, 1)],
        ),
        lambda: operations.request_calibration(
            actor,
            tenant_id,
            region_id,
            plant_id,
            configuration_id=1,
            start_date=(NOW - timedelta(days=10)).date(),
            end_date=NOW.date(),
        ),
        lambda: operations.activate_calibration(actor, tenant_id, region_id, plant_id, 1),
        lambda: operations.reset_calibration(actor, tenant_id, region_id, plant_id),
        lambda: set_calibration_automation(actor, tenant_id, region_id, plant_id, enabled=False),
        lambda: save_incident(actor, tenant_id, region_id, plant_id),
        lambda: finish_incident(
            actor, tenant_id, region_id, plant_id, incident_id=1, expected_revision=1, cancel=True
        ),
        lambda: configure_schedule(actor, tenant_id, region_id, enabled=False, times=[], now=NOW),
        lambda: configure_reminders(
            actor,
            tenant_id,
            region_id,
            language="en",
            enabled=False,
            recipients=[],
            times=[],
            now=NOW,
        ),
        lambda: trash.trash_region(actor, tenant_id, region_id),
        lambda: trash.restore_region(actor, tenant_id, region_id),
        lambda: trash.trash_plant(actor, tenant_id, region_id, plant_id),
        lambda: trash.restore_plant(actor, tenant_id, region_id, plant_id),
    ]


@pytest.mark.parametrize("identity", ["viewer", "outsider", "platform", "demoted", "inactive"])
def test_application_mutations_require_current_workspace_administrator(identity: str) -> None:
    system = make_system(approved=True)
    plant = operations.create_plant(
        system.member, system.tenant.pk, system.region.pk, name="PV", dc_capacity_mw=1
    )
    if identity in {"demoted", "inactive"}:
        actor = system.member
        if identity == "demoted":
            Membership.objects.filter(user=actor).update(role=Membership.Role.VIEWER)
        else:
            User.objects.filter(pk=actor.pk).update(is_active=False)
    else:
        actor = User.objects.create_user(username=identity, is_superuser=identity == "platform")
        if identity == "viewer":
            Membership.objects.create(user=actor, tenant=system.tenant)
    before = (Region.objects.count(), Plant.objects.count(), AuditEvent.objects.count())
    for mutate in mutation_calls(system, actor, plant):
        with pytest.raises(PermissionDenied):
            mutate()
    assert (Region.objects.count(), Plant.objects.count(), AuditEvent.objects.count()) == before
    assert CollectionJob.objects.filter(status="pending").count() == 1
    assert not CollectionSchedule.objects.exists()
    assert not ReminderSchedule.objects.exists()
    assert PlantEvent.objects.count() == 1


def test_viewer_reads_shared_records_and_changes_own_language() -> None:
    system = make_system()
    viewer = User.objects.create_user(username="viewer")
    Membership.objects.create(user=viewer, tenant=system.tenant)
    client = Client()
    client.force_login(viewer)
    base = f"/api/tenants/{system.tenant.pk}/"
    for suffix in (
        "sites/",
        f"regions/{system.region.pk}/location-change-requests/",
        "trash/regions/",
        "trash/plants/",
    ):
        assert client.get(base + suffix).status_code == 200
    response = client.post(
        "/api/session/language/", {"language": "tr"}, content_type="application/json"
    )
    assert response.status_code == 200


def test_workspace_transfer_serializes_with_mutation_and_rechecks_waiting_writer() -> None:
    system = make_system()
    replacement = User.objects.create_user(username="replacement")
    Membership.objects.create(user=replacement, tenant=system.tenant)
    transferring, writer_started = Event(), Event()

    def transfer() -> None:
        close_old_connections()
        try:
            with transaction.atomic():
                Tenant.objects.select_for_update().get(pk=system.tenant.pk)
                reassign_workspace_admin(
                    system.admin,
                    system.tenant.pk,
                    replacement.pk,
                    expected_admin_id=system.member.pk,
                )
                transferring.set()
                assert writer_started.wait(timeout=10)
        finally:
            connections.close_all()

    def mutate() -> None:
        close_old_connections()
        try:
            assert transferring.wait(timeout=10)
            writer_started.set()
            with pytest.raises(PermissionDenied):
                request_location_change(
                    system.member,
                    system.region.pk,
                    system.tenant.pk,
                    expected_revision=1,
                    latitude=41,
                    longitude=30,
                    timezone="UTC",
                )
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        transfer_future, mutation_future = pool.submit(transfer), pool.submit(mutate)
        transfer_future.result(timeout=15)
        mutation_future.result(timeout=15)
    assert not RegionLocationChangeRequest.objects.exists()
    assert Membership.objects.get(user=replacement).role == Membership.Role.ADMIN
