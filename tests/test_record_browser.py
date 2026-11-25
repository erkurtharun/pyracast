import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from pyracast.accounts.models import WorkspaceAdminChange, WorkspaceMemberRemoval
from pyracast.regions.audit import record_audit
from pyracast.regions.models import AuditAction, AuditEvent, Region
from pyracast.regions.operations import submit_region
from tests.support.api import verified_admin
from tests.support.domain import System, make_system

pytestmark = pytest.mark.django_db

CHANGELISTS = (
    "regions_auditevent",
    "plants_plantevent",
    "plants_plant",
    "connections_sitebinding",
    "forecasts_collectionjob",
    "forecasts_requestattempt",
    "notifications_notificationdelivery",
    "accounts_workspaceadminchange",
    "accounts_workspacememberremoval",
)


def renamed(system: System, region: Region) -> None:
    record_audit(actor=system.member, region=region, action=AuditAction.REGION_RENAMED, revision=1)


@pytest.mark.parametrize("name", CHANGELISTS)
def test_each_record_list_opens_for_the_operator(name: str) -> None:
    make_system()
    client, _ = verified_admin()

    assert client.get(reverse(f"admin:{name}_changelist")).status_code == 200


def test_the_plant_page_links_to_its_records_filtered_to_the_plant() -> None:
    system = make_system()
    other = submit_region(
        system.member, system.tenant.pk, name="Other", latitude=41, longitude=30, timezone="UTC"
    )
    renamed(system, system.region)
    renamed(system, other)
    client, _ = verified_admin()

    page = client.get(reverse("admin:workspace_plant", args=(system.region.pk,))).content.decode()
    link = f"{reverse('admin:regions_auditevent_changelist')}?region={system.region.pk}"
    listed = client.get(link)

    assert link in page
    assert listed.status_code == 200
    assert {event.region_id for event in listed.context["cl"].result_list} == {system.region.pk}


def test_a_link_naming_no_live_location_filters_nothing() -> None:
    system = make_system()
    renamed(system, system.region)
    client, _ = verified_admin()

    listed = client.get(f"{reverse('admin:regions_auditevent_changelist')}?region=abc")

    assert listed.status_code == 200
    assert len(listed.context["cl"].result_list) == AuditEvent.objects.count()


def test_the_audit_list_reads_a_constant_number_of_queries() -> None:
    system = make_system()
    client, _ = verified_admin()
    url = reverse("admin:regions_auditevent_changelist")
    renamed(system, system.region)
    client.get(url)
    with CaptureQueriesContext(connection) as few:
        client.get(url)
    for _ in range(5):
        renamed(system, system.region)
    with CaptureQueriesContext(connection) as many:
        client.get(url)

    assert len(many.captured_queries) == len(few.captured_queries)


def test_ledger_rows_name_their_workspace_change() -> None:
    system = make_system()
    other = User.objects.create_user(username="other")
    change = WorkspaceAdminChange.objects.create(
        tenant=system.tenant, actor=system.admin, administrator=system.member
    )
    removal = WorkspaceMemberRemoval.objects.create(
        tenant=system.tenant, actor=system.admin, user=other
    )

    assert str(change) == (
        f"Workspace administrator of workspace {system.tenant.pk} set to {system.member.pk}"
    )
    assert str(removal) == f"Member {other.pk} removed from workspace {system.tenant.pk}"
