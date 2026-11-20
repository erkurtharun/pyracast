from datetime import datetime
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from pyracast.accounts.models import Membership, Tenant, WorkspaceRole
from pyracast.errors import ConflictError
from pyracast.forecasts.validation import validate_binding
from pyracast.plants.approval import ReviewDecision, approve_region, review_snapshot
from pyracast.plants.models import PlantSubmission
from pyracast.regions.models import AuditAction, AuditEvent, Region, RegionStatus
from pyracast.regions.operations import submit_region
from tests.support.admin_setup import admin_system, submitted_plant
from tests.support.api import verified_admin
from tests.support.concurrency import behind_open_transaction
from tests.support.domain import FakeAdapter, System, make_system

pytestmark = pytest.mark.django_db(transaction=True)

REASON = "The declared capacity does not match the grid licence."
STATUS_CHANGED = (
    "The review status changed after this page opened. Reload the plant and review it again."
)


def decide(system: System, decision: str, reason: str = REASON) -> bool:
    from pyracast.plants.approval import decide_review

    return decide_review(system.admin, system.region.pk, decision, reason=reason)


def resubmit(actor: User, system: System, **body: Any) -> Any:
    from pyracast.plants.approval import resubmit_region

    expected = body.get("expected_revision", Region.objects.get(pk=system.region.pk).revision)
    return resubmit_region(actor, system.tenant.pk, system.region.pk, expected_revision=expected)


def validated(system: System) -> System:
    validate_binding(system.admin, system.binding.pk, FakeAdapter(), timezone.now())
    return system


def member_client(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def workspace_member(tenant: Tenant, username: str, role: str = WorkspaceRole.VIEWER) -> User:
    user = User.objects.create_user(username=username)
    Membership.objects.create(tenant=tenant, user=user, role=role)
    return user


def sites(client: Client, tenant: Tenant) -> tuple[int, Any]:
    response = client.get(f"/api/tenants/{tenant.pk}/sites/")
    return response.status_code, response.json()


def site_row(client: Client, system: System) -> dict[str, Any]:
    status, body = sites(client, system.tenant)
    assert status == 200, body
    return next(row for row in body["results"] if row["id"] == system.region.pk)


def resubmit_url(system: System) -> str:
    return f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/resubmit/"


def plant_url(system: System, plant_id: int) -> str:
    return f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant_id}/"


def decision_post(region: Region, action: str, **fields: object) -> dict[str, object]:
    region.refresh_from_db()
    return {
        "expected_revision": region.revision,
        "expected_status": region.status,
        "action": action,
        **fields,
    }


def console_notices(client: Client, url: str, data: dict[str, object]) -> list[str]:
    response = client.post(url, data, follow=True)
    return [str(message) for message in response.context["messages"]]


def approval_post(client: Client, region: Region) -> dict[str, object]:
    form = client.get(reverse("admin:workspace_plant", args=(region.pk,))).context["action_form"]
    values = {name: form[name].value() for name in form.fields}
    return {
        **{name: value for name, value in values.items() if value is not None},
        "action": "approve",
    }


def status_of(region: Region) -> str:
    return Region.objects.get(pk=region.pk).status


@pytest.mark.parametrize(
    ("approved", "decision", "status", "action"),
    [
        (False, ReviewDecision.REJECT, RegionStatus.REJECTED, AuditAction.REGION_REJECTED),
        (True, ReviewDecision.SUSPEND, RegionStatus.SUSPENDED, AuditAction.REGION_SUSPENDED),
    ],
)
def test_a_review_decision_changes_the_status_and_writes_one_audit_line(
    approved: bool, decision: str, status: str, action: str
) -> None:
    system = make_system(approved=approved)
    client, operator = verified_admin()

    client.post(
        reverse("admin:workspace_decision", args=(system.region.pk,)),
        decision_post(system.region, decision, decision_reason=REASON),
    )

    region = Region.objects.get(pk=system.region.pk)
    assert region.status == status
    assert (region.approved_binding_id is not None) == approved
    event = AuditEvent.objects.get(region=region, action=action)
    assert (event.actor_id, event.revision) == (operator.pk, region.revision)


def test_the_customer_read_keeps_its_region_fields() -> None:
    system = make_system()
    client = member_client(system.member)

    row = site_row(client, system)

    assert {"id", "name", "latitude", "longitude", "timezone", "revision", "status", "plant"} <= (
        row.keys()
    )
    assert row["status"] == RegionStatus.PENDING


@pytest.mark.parametrize("reason", ["", "   "])
@pytest.mark.parametrize(
    ("approved", "decision"), [(False, ReviewDecision.REJECT), (True, ReviewDecision.SUSPEND)]
)
def test_a_rejection_or_suspension_without_a_reason_is_refused(
    approved: bool, decision: str, reason: str
) -> None:
    system = make_system(approved=approved)
    before = status_of(system.region)

    with pytest.raises(ValidationError) as refused:
        decide(system, decision, reason)

    assert refused.value.code == "review_reason_required"
    assert status_of(system.region) == before
    assert not AuditEvent.objects.filter(
        action__in=(AuditAction.REGION_REJECTED, AuditAction.REGION_SUSPENDED)
    ).exists()


def test_a_reason_longer_than_500_characters_is_refused() -> None:
    system = make_system()

    with pytest.raises(ValidationError):
        decide(system, ReviewDecision.REJECT, "x" * 501)

    assert status_of(system.region) == RegionStatus.PENDING


@pytest.mark.parametrize(
    ("approved", "decision", "action"),
    [
        (False, ReviewDecision.REJECT, AuditAction.REGION_REJECTED),
        (True, ReviewDecision.SUSPEND, AuditAction.REGION_SUSPENDED),
    ],
)
def test_the_reason_is_stored_with_its_decision(approved: bool, decision: str, action: str) -> None:
    system = make_system(approved=approved)

    decide(system, decision, f"  {REASON}  ")

    assert AuditEvent.objects.get(action=action).details == {"reason": REASON}


def test_members_read_the_reason_and_time_of_the_decision_of_their_own_plant_only() -> None:
    system = make_system()
    viewer = workspace_member(system.tenant, "viewer")
    other_tenant = Tenant.objects.create(name="Other workspace")
    other_member = workspace_member(other_tenant, "other-admin", WorkspaceRole.ADMIN)
    other_region = submit_region(
        other_member, other_tenant.pk, name="Other", latitude=41, longitude=30, timezone="UTC"
    )
    from pyracast.plants.approval import decide_review

    decide_review(
        system.admin, other_region.pk, ReviewDecision.REJECT, reason="Other workspace reason"
    )
    before = timezone.now()
    decide(system, ReviewDecision.REJECT)

    for user in (system.member, viewer):
        row = site_row(member_client(user), system)
        assert (row["status"], row["review_reason"]) == (RegionStatus.REJECTED, REASON)
        assert datetime.fromisoformat(row["reviewed_at"]) >= before
    _, body = sites(member_client(system.member), system.tenant)
    assert "Other workspace reason" not in str(body)
    assert sites(member_client(other_member), system.tenant)[0] == 403
    outsider = User.objects.create_user(username="outsider")
    assert sites(member_client(outsider), system.tenant)[0] == 403


def test_an_undecided_plant_has_no_review_and_an_approval_has_no_reason() -> None:
    system = make_system()
    client = member_client(system.member)
    assert (site_row(client, system)["review_reason"], site_row(client, system)["reviewed_at"]) == (
        None,
        None,
    )
    validated(system)

    approve_region(system.admin, system.region.pk, system.binding.pk, timezone.now())

    row = site_row(client, system)
    assert row["review_reason"] is None
    assert row["reviewed_at"] is not None


def test_the_console_refuses_a_rejection_without_a_reason() -> None:
    system = admin_system()
    client, _ = verified_admin()
    url = reverse("admin:workspace_decision", args=(system.region.pk,))

    refused = console_notices(client, url, decision_post(system.region, "reject"))
    done = console_notices(
        client, url, decision_post(system.region, "reject", decision_reason=REASON)
    )

    assert refused == ["Enter the reason the customer will read."]
    assert done == ["Plant rejected."]
    assert AuditEvent.objects.get(action=AuditAction.REGION_REJECTED).details == {"reason": REASON}


def test_the_console_asks_for_the_reason_and_shows_the_earlier_one_on_a_resubmission() -> None:
    system = admin_system()
    submitted_plant(system)
    client, _ = verified_admin()
    page = reverse("admin:workspace_plant", args=(system.region.pk,))

    asked = client.get(page).content.decode()
    decide(system, ReviewDecision.REJECT)
    resubmit(system.member, system)
    shown = client.get(page).content.decode()

    assert 'name="decision_reason" required' in asked
    assert REASON in shown
    assert "Resubmitted" in shown


def test_a_rejected_plant_accepts_edits_of_its_declaration() -> None:
    system = admin_system()
    plant = submitted_plant(system)
    decide(system, ReviewDecision.REJECT)
    client = member_client(system.member)

    response = client.put(
        plant_url(system, plant.pk),
        {"name": plant.name, "dc_capacity_mw": 5.5},
        content_type="application/json",
    )

    assert response.status_code == 200, response.content
    assert PlantSubmission.objects.get(plant=plant).dc_capacity_mw == 5.5
    assert status_of(system.region) == RegionStatus.REJECTED


def test_a_rejected_plant_is_resubmitted_through_the_review_table_with_an_audit_line() -> None:
    system = admin_system()
    submitted_plant(system)
    decide(system, ReviewDecision.REJECT)
    region = Region.objects.get(pk=system.region.pk)
    client = member_client(system.member)

    response = client.post(
        resubmit_url(system),
        {"expected_revision": region.revision},
        content_type="application/json",
    )
    again = client.post(
        resubmit_url(system),
        {"expected_revision": region.revision},
        content_type="application/json",
    )

    assert response.status_code == 200, response.content
    assert response.json()["status"] == RegionStatus.PENDING
    assert response.json()["review_reason"] == REASON
    assert (again.status_code, again.json()["status"]) == (200, RegionStatus.PENDING)
    events = AuditEvent.objects.filter(action=AuditAction.REGION_RESUBMITTED)
    assert [(event.actor_id, event.revision) for event in events] == [
        (system.member.pk, region.revision)
    ]


@pytest.mark.parametrize("status", [RegionStatus.SUSPENDED, RegionStatus.APPROVED])
def test_only_a_rejected_plant_can_be_resubmitted(status: str) -> None:
    system = make_system(approved=True)
    if status == RegionStatus.SUSPENDED:
        decide(system, ReviewDecision.SUSPEND)
    client = member_client(system.member)

    response = client.post(
        resubmit_url(system),
        {"expected_revision": Region.objects.get(pk=system.region.pk).revision},
        content_type="application/json",
    )

    assert response.status_code == 409
    assert response.json()["code"] == "conflict"
    assert status_of(system.region) == status
    assert not AuditEvent.objects.filter(action=AuditAction.REGION_RESUBMITTED).exists()


def test_a_resubmission_from_a_stale_page_is_refused() -> None:
    system = make_system()
    decide(system, ReviewDecision.REJECT)
    Region.objects.filter(pk=system.region.pk).update(revision=2)

    response = member_client(system.member).post(
        resubmit_url(system), {"expected_revision": 1}, content_type="application/json"
    )

    assert (response.status_code, response.json()["code"]) == (409, "region_revision_changed")
    assert status_of(system.region) == RegionStatus.REJECTED


def test_only_a_workspace_administrator_can_resubmit() -> None:
    system = make_system()
    decide(system, ReviewDecision.REJECT)
    viewer = workspace_member(system.tenant, "viewer")
    outsider = User.objects.create_user(username="outsider")
    body = {"expected_revision": 1}

    viewer_answer = member_client(viewer).post(
        resubmit_url(system), body, content_type="application/json"
    )
    outsider_answer = member_client(outsider).post(
        resubmit_url(system), body, content_type="application/json"
    )

    assert (viewer_answer.status_code, viewer_answer.json()["code"]) == (
        403,
        "workspace_admin_required",
    )
    assert (outsider_answer.status_code, outsider_answer.json()["code"]) == (
        403,
        "membership_required",
    )
    assert status_of(system.region) == RegionStatus.REJECTED


def test_a_resubmission_racing_an_approval_from_the_rejected_page_never_approves() -> None:
    system = validated(admin_system())
    submitted_plant(system)
    decide(system, ReviewDecision.REJECT)
    seen = review_snapshot(system.region.pk)

    def approve() -> str:
        try:
            approve_region(
                system.admin, system.region.pk, system.binding.pk, timezone.now(), seen=seen
            )
        except ConflictError as refusal:
            return str(refusal.message)
        return "approved"

    resubmitted, approval = behind_open_transaction(
        lambda: resubmit(system.member, system), approve
    )

    assert (resubmitted, approval) == (True, STATUS_CHANGED)
    assert status_of(system.region) == RegionStatus.PENDING
    assert not AuditEvent.objects.filter(action=AuditAction.REGION_APPROVED).exists()


def test_a_stale_rejection_does_not_undo_a_concurrent_resubmission() -> None:
    system = make_system()
    decide(system, ReviewDecision.REJECT)
    from pyracast.plants.approval import decide_review

    seen = review_snapshot(system.region.pk)

    def reject_again() -> str:
        try:
            decide_review(
                system.admin, system.region.pk, ReviewDecision.REJECT, reason=REASON, seen=seen
            )
        except ConflictError as refusal:
            return str(refusal.message)
        return "rejected"

    resubmitted, rejection = behind_open_transaction(
        lambda: resubmit(system.member, system), reject_again
    )

    assert (resubmitted, rejection) == (True, STATUS_CHANGED)
    assert status_of(system.region) == RegionStatus.PENDING
    assert AuditEvent.objects.filter(action=AuditAction.REGION_REJECTED).count() == 1


def test_two_concurrent_resubmissions_reopen_the_review_once() -> None:
    system = make_system()
    decide(system, ReviewDecision.REJECT)

    first, other = behind_open_transaction(
        lambda: resubmit(system.member, system), lambda: resubmit(system.member, system)
    )

    assert sorted([first, other]) == [False, True]
    assert AuditEvent.objects.filter(action=AuditAction.REGION_RESUBMITTED).count() == 1


def test_reject_read_edit_resubmit_and_approve_through_the_real_api() -> None:
    system = validated(admin_system())
    plant = submitted_plant(system)
    operator, _ = verified_admin()
    customer = member_client(system.member)

    rejected = console_notices(
        operator,
        reverse("admin:workspace_decision", args=(system.region.pk,)),
        decision_post(system.region, "reject", decision_reason=REASON),
    )
    read = site_row(customer, system)
    edited = customer.put(
        plant_url(system, plant.pk),
        {"name": plant.name, "dc_capacity_mw": 3.9, "inverter_capacity_mw": 3.5},
        content_type="application/json",
    )
    resubmitted = customer.post(
        resubmit_url(system),
        {"expected_revision": read["revision"]},
        content_type="application/json",
    )
    approved = console_notices(
        operator,
        reverse("admin:workspace_plant", args=(system.region.pk,)),
        approval_post(operator, system.region),
    )
    final = site_row(customer, system)

    assert rejected == ["Plant rejected."]
    assert (read["status"], read["review_reason"]) == (RegionStatus.REJECTED, REASON)
    assert edited.status_code == 200, edited.content
    assert (resubmitted.status_code, resubmitted.json()["status"]) == (200, RegionStatus.PENDING)
    assert approved == ["Plant approved. The first saved forecast is being prepared."]
    assert (final["status"], final["review_reason"]) == (RegionStatus.APPROVED, None)
    assert final["plant"]["current_configuration"]["dc_capacity_mw"] == 3.9


def test_a_rolled_back_resubmission_leaves_the_plant_rejected() -> None:
    system = make_system()
    decide(system, ReviewDecision.REJECT)

    def rolled_back() -> None:
        with transaction.atomic():
            resubmit(system.member, system)
            raise RuntimeError

    with pytest.raises(RuntimeError):
        rolled_back()

    assert status_of(system.region) == RegionStatus.REJECTED
    assert not AuditEvent.objects.filter(action=AuditAction.REGION_RESUBMITTED).exists()
